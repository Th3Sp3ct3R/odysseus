# src/deep_research_v2.py
"""
Deep Research v2: Claims-based iterative research engine.

Think->Search->Extract->Synthesize->Decide loop with:
- Structured claim extraction (atomic facts, not prose)
- Claims database with dedup, corroboration, contradiction detection
- Per-sub-question confidence tracking
- Source diversity enforcement
- Temporal awareness
"""
import asyncio
import json
import logging
import re
import time
from typing import Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

from src.research_utils import strip_thinking, is_low_quality
from src.structured_extractor import STRUCTURED_EXTRACTOR_PROMPT, parse_structured_response
from src.claims_database import ClaimsDatabase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
RESEARCH_PLAN_PROMPT = """\
You are a research strategist. Analyze this question and create a research plan.

**Question:** {question}

Break this down:
1. Key sub-topics to cover
2. Specific data points to look for
3. What a complete answer includes

Return JSON:
{{
  "sub_questions": ["sub-question 1", "sub-question 2", ...],
  "key_topics": ["topic 1", ...],
  "success_criteria": "One sentence describing a complete answer"
}}

Aim for 3-6 sub-questions."""

QUERY_GEN_PROMPT = """\
You are a research assistant planning web searches.

**Original question:** {question}

**Research plan:**
{research_plan}

**Current claims database:**
{claims_summary}

**Round:** {round_num}

Generate {num_queries} focused search queries.
{round_instruction}

Return ONLY a JSON array of query strings.
Example: ["query one", "query two", "query three"]"""

SYNTHESIZE_PROMPT = """\
You are writing a comprehensive research report.

**Original question:** {question}

**All collected claims and evidence:**
{claims_database}

Integrate these claims into a well-organized report. Remove redundancy, resolve contradictions, and maintain logical flow. Include source URLs as inline citations [like this](url).

Write only the report -- no preamble."""

STOP_PROMPT = """\
Decide if research is comprehensive enough.

**Original question:** {question}

**Claims database stats:**
{stats}

**Sub-question coverage:**
{coverage}

Are all sub-questions answered with high-confidence claims? Reply ONLY "YES" or "NO" followed by a brief reason."""

FINAL_REPORT_PROMPT = """\
Write a comprehensive, detailed research report.

**Question:** {question}

**All claims and evidence:**
{claims_database}

Requirements:
- Minimum 1500 words
- Use ## headings and ### subheadings
- Multiple detailed paragraphs per section
- Synthesize information, explain WHY things matter
- Include specific data points and statistics
- Include source URLs as [like this](url) citations
- Note where sources agree/disagree
- Add executive summary at top
- End with clear conclusion
- Engaging, informative style"""

CATEGORY_PROMPTS = {
    "product": """PRODUCT research:
- Structured as RANKED LIST (best first)
- For each: name, price, summary, pros, cons, where to buy
- Comparison table at start
- Verdict section at end""",
    
    "comparison": """COMPARISON report:
- Comparison table (criteria x options)
- Section per option with strengths/weaknesses
- Best For verdicts
- Shared considerations""",
    
    "howto": """HOW-TO guide:
- Quick guide (numbered list, concise)
- Prerequisites
- Detailed steps with headings
- Tips and warnings in blockquotes
- Common mistakes section""",
    
    "factcheck": """FACT-CHECK report:
- The Claim section
- Evidence For and Evidence Against sections
- Verdict: Supported / Mixed / Unsupported
- Nuance & Caveats""",
}


class DeepResearcherV2:
    """Claims-based iterative research engine."""
    
    def __init__(
        self,
        llm_endpoint: str,
        llm_model: str,
        llm_headers: Optional[Dict] = None,
        max_rounds: int = 8,
        max_time: int = 300,
        max_urls_per_round: int = 3,
        max_content_chars: int = 15000,
        max_report_tokens: int = 8192,
        min_rounds: int = 2,
        max_empty_rounds: int = 2,
        synthesis_window: int = 10,
        progress_callback: Optional[Callable] = None,
        search_provider: Optional[str] = None,
        category: Optional[str] = None,
    ):
        self.llm_endpoint = llm_endpoint
        self.llm_model = llm_model
        self.llm_headers = llm_headers
        self.search_provider_override = search_provider
        self.category = category
        
        self.max_rounds = max_rounds
        self.max_time = max_time
        self.max_urls_per_round = max_urls_per_round
        self.max_content_chars = max_content_chars
        self.max_report_tokens = max_report_tokens
        self.min_rounds = min_rounds
        self.max_empty_rounds = max_empty_rounds
        self.synthesis_window = synthesis_window
        
        self._progress = progress_callback
        self._cancelled = False
        self._start_time = 0.0
        
        self.queries_used: Set[str] = set()
        self.urls_fetched: Set[str] = set()
        self.providers_used: List[str] = []
        self.round_count = 0
        
        # NEW: Claims database
        self.claims_db = ClaimsDatabase()
        
        # Sub-questions from research plan
        self.sub_questions: List[str] = []
        
        # Findings export (for backward compat with handler)
        self.findings: List[Dict] = []
        self.evolving_report = ""
        self.research_plan = ""
    
    def cancel(self):
        """Request cancellation."""
        self._cancelled = True
    
    def _emit(self, **kwargs):
        """Emit progress event."""
        if self._progress:
            try:
                self._progress(kwargs)
            except Exception:
                pass
    
    def _time_exceeded(self) -> bool:
        """Check if time limit exceeded."""
        return (time.time() - self._start_time) > self.max_time
    
    # ------------------------------------------------------------------
    # LLM helper
    # ------------------------------------------------------------------
    async def _llm(self, messages: List[Dict], temperature: float = 0.3,
                   max_tokens: int = 4096, timeout: int = 60) -> str:
        """Call LLM and return stripped text."""
        from src.llm_core import llm_call_async
        response = await llm_call_async(
            url=self.llm_endpoint,
            model=self.llm_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            headers=self.llm_headers,
            timeout=timeout,
        )
        return strip_thinking(response)
    
    def _parse_json_array(self, text: str) -> List:
        """Extract JSON array from text."""
        if not text:
            return []
        clean = text.strip()
        if clean.startswith("```"):
            clean = re.sub(r'^```(?:json)?\s*', '', clean, flags=re.IGNORECASE)
            clean = re.sub(r'\s*```$', '', clean)
        match = re.search(r'^\s*\[[\s\S]*\]\s*$', clean, re.MULTILINE)
        if not match:
            match = re.search(r'\[[\s\S]*\]', clean)
        if not match:
            return []
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return []
    
    def _parse_json_object(self, text: str) -> Optional[Dict]:
        """Extract JSON object from text."""
        if not text:
            return None
        clean = text.strip()
        if clean.startswith("```"):
            clean = re.sub(r'^```(?:json)?\s*', '', clean, flags=re.IGNORECASE)
            clean = re.sub(r'\s*```$', '', clean)
        match = re.search(r'\{[\s\S]*\}', clean)
        if not match:
            return None
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None
    
    def _strip_code_block(self, text: str) -> str:
        """Remove markdown code fences."""
        return re.sub(r'^```\w*\s*\n?', '', text, flags=re.MULTILINE).strip()
    
    # ------------------------------------------------------------------
    # PLAN
    # ------------------------------------------------------------------
    async def _create_plan(self, question: str) -> str:
        """Create research plan with sub-questions."""
        prompt = RESEARCH_PLAN_PROMPT.format(question=question)
        try:
            response = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                timeout=30,
            )
            parsed = self._parse_json_object(response)
            if parsed:
                parts = []
                if parsed.get("sub_questions"):
                    self.sub_questions = [q.strip() for q in parsed["sub_questions"] if q.strip()]
                    parts.append("Sub-questions: " + "; ".join(self.sub_questions))
                if parsed.get("key_topics"):
                    parts.append("Key topics: " + ", ".join(parsed["key_topics"]))
                if parsed.get("success_criteria"):
                    parts.append("Success: " + parsed["success_criteria"])
                return "\n".join(parts) if parts else response
            return response
        except Exception as e:
            logger.warning(f"Research planning failed: {e}")
            return ""
    
    async def _classify_category(self, question: str) -> Optional[str]:
        """Classify question into category."""
        valid = ", ".join(CATEGORY_PROMPTS.keys())
        prompt = (
            f"Classify this research question into exactly ONE category.\n"
            f"Categories: {valid}\n"
            f"If none fit, respond: general\n\n"
            f"Question: {question}\n\n"
            f"Respond ONLY with the category name."
        )
        try:
            result = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0, max_tokens=20, timeout=15,
            )
            cat = (result or "").strip().lower()
            first = cat.split()[0].strip('.,"*:') if cat.split() else ""
            if first in CATEGORY_PROMPTS:
                return first
            for c in CATEGORY_PROMPTS:
                if c in cat:
                    return c
            return None
        except Exception:
            return None
    
    # ------------------------------------------------------------------
    # THINK
    # ------------------------------------------------------------------
    async def _generate_queries(self, question: str, round_num: int) -> List[str]:
        """Generate search queries targeting gaps in claims database."""
        if round_num == 1:
            num_queries = 4
            round_instruction = "First round -- generate broad, diverse queries."
        else:
            num_queries = 3
            round_instruction = (
                "Target gaps: which sub-questions have low confidence? "
                "What claims need corroboration? What perspectives are missing?"
            )
        
        # Build claims summary showing coverage gaps
        claims_summary = self.claims_db.get_claims_for_synthesis()
        if self.sub_questions:
            coverage = []
            for sq in self.sub_questions:
                conf = self.claims_db.get_confidence_for_sub_question(sq)
                coverage.append(f"- {sq}: {conf:.2f} confidence")
            claims_summary += "\n\n**Sub-question coverage:**\n" + "\n".join(coverage)
        
        prompt = QUERY_GEN_PROMPT.format(
            question=question,
            research_plan=self.research_plan or "(No plan -- search broadly.)",
            claims_summary=claims_summary,
            round_num=round_num,
            num_queries=num_queries,
            round_instruction=round_instruction,
        )
        
        try:
            response = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=4096,
            )
            queries = self._parse_json_array(response)
            new_queries = [q for q in queries if q not in self.queries_used]
            self.queries_used.update(new_queries)
            logger.info(f"Round {round_num} queries: {new_queries}")
            return new_queries
        except Exception as e:
            logger.error(f"Query generation failed: {e}")
            return []
    
    # ------------------------------------------------------------------
    # SEARCH + EXTRACT
    # ------------------------------------------------------------------
    async def _search_and_extract(self, queries: List[str],
                                  question: str) -> List[Dict]:
        """Parallel search + claim extraction with source diversity."""
        from src.search.core import search_all
        from src.search.content import fetch_and_extract
        
        all_fetch_tasks = []
        seen_urls = set()
        
        async def search_one_query(query: str):
            """Search for one query, return results."""
            try:
                results = await search_all(
                    query,
                    num_results=8,
                    provider=self.search_provider_override,
                )
                return results.get("results", [])
            except Exception as e:
                logger.warning(f"Search failed for '{query}': {e}")
                return []
        
        # Parallel search across all queries
        search_coros = [search_one_query(q) for q in queries]
        search_results = await asyncio.gather(*search_coros)
        
        # Flatten and dedupe
        for results in search_results:
            for r in results:
                url = r.get("url", "").rstrip("/")
                if url and url not in self.urls_fetched and url not in seen_urls:
                    seen_urls.add(url)
                    all_fetch_tasks.append((url, r))
                    if len(all_fetch_tasks) >= self.max_urls_per_round * len(queries):
                        break
        
        if not all_fetch_tasks:
            return []
        
        # Parallel fetch + extract
        async def fetch_and_extract_claims(url: str, result: dict, round_num: int):
            """Fetch URL and extract structured claims."""
            try:
                # Fetch content
                content_data = await fetch_and_extract(
                    url,
                    max_chars=self.max_content_chars,
                    timeout=20,
                )
                if not content_data or not content_data.get("content"):
                    return None, None
                
                content = content_data["content"]
                title = result.get("title") or content_data.get("title", "")
                
                # Track provider
                provider = result.get("provider", "")
                if provider and provider not in self.providers_used:
                    self.providers_used.append(provider)
                
                # Extract structured claims
                goal = f"Research question: {question}"
                extract_prompt = STRUCTURED_EXTRACTOR_PROMPT.format(
                    webpage_content=content[:self.max_content_chars],
                    goal=goal,
                )
                
                response = await self._llm(
                    [{"role": "user", "content": extract_prompt}],
                    temperature=0.1,
                    max_tokens=4096,
                    timeout=45,
                )
                
                claims = parse_structured_response(response)
                
                # Tag claims with source
                for c in claims:
                    c["source_url"] = url
                    c["source_title"] = title
                
                # Return both legacy format and claims
                legacy_result = {
                    "url": url,
                    "title": title,
                    "summary": content_data.get("summary", "")[:1000],
                    "evidence": content[:2000],
                    "og_image": content_data.get("og_image", ""),
                }
                
                return legacy_result, claims
            
            except Exception as e:
                logger.warning(f"Fetch/extract failed for {url}: {e}")
                return None, None
        
        # Run fetch+extract in parallel
        fetch_coros = [
            fetch_and_extract_claims(url, result, self.round_count)
            for url, result in all_fetch_tasks
        ]
        results = await asyncio.gather(*fetch_coros)
        
        # Process results
        findings = []
        total_new_claims = 0
        total_merged = 0
        
        for legacy_result, claims in results:
            if legacy_result:
                findings.append(legacy_result)
                self.urls_fetched.add(legacy_result["url"])
            
            if claims:
                # Add to claims database
                sq_match = self._match_sub_question(legacy_result.get("summary", "") if legacy_result else "")
                merged, added = self.claims_db.add_claims(
                    claims,
                    round_num=self.round_count,
                    sub_question=sq_match,
                )
                total_new_claims += added
                total_merged += merged
        
        logger.info(
            f"Round {self.round_count}: {len(findings)} pages fetched, "
            f"{total_new_claims} new claims, {total_merged} merged"
        )
        
        return findings
    
    def _match_sub_question(self, text: str) -> str:
        """Match text to most relevant sub-question."""
        if not self.sub_questions:
            return ""
        text_lower = text.lower()
        best_match = ""
        best_score = 0
        for sq in self.sub_questions:
            # Simple keyword overlap
            sq_words = set(sq.lower().split())
            text_words = set(text_lower.split())
            overlap = len(sq_words & text_words)
            if overlap > best_score:
                best_score = overlap
                best_match = sq
        return best_match if best_score > 2 else ""
    
    # ------------------------------------------------------------------
    # SYNTHESIZE
    # ------------------------------------------------------------------
    async def _synthesize(self, question: str) -> str:
        """Synthesize claims database into report."""
        # Limit synthesis window
        recent_claims = self.claims_db.claims[-self.synthesis_window:]
        
        # Build synthesis view
        claims_view = self.claims_db.get_claims_for_synthesis()
        
        prompt = SYNTHESIZE_PROMPT.format(
            question=question,
            claims_database=claims_view[:12000],
        )
        
        try:
            response = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=self.max_report_tokens,
                timeout=90,
            )
            return strip_thinking(response)
        except Exception as e:
            logger.error(f"Synthesis failed: {e}")
            return self.evolving_report
    
    # ------------------------------------------------------------------
    # DECIDE
    # ------------------------------------------------------------------
    async def _should_stop(self, question: str, round_num: int) -> bool:
        """Decide if research is comprehensive enough."""
        # Check per-sub-question confidence
        if self.sub_questions:
            all_high_conf = True
            for sq in self.sub_questions:
                conf = self.claims_db.get_confidence_for_sub_question(sq)
                if conf < 0.75:
                    all_high_conf = False
                    break
            
            if all_high_conf and round_num >= self.min_rounds:
                logger.info(f"All sub-questions have high confidence, stopping")
                return True
        
        # Fallback: ask LLM
        stats = self.claims_db.get_stats()
        coverage = self.claims_db.get_claims_for_synthesis()[:2000]
        
        prompt = STOP_PROMPT.format(
            question=question,
            stats=json.dumps(stats, indent=2),
            coverage=coverage,
        )
        
        try:
            response = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=200,
                timeout=30,
            )
            clean = strip_thinking(response).strip().upper()
            stop = clean.startswith("YES")
            if stop:
                reason = clean[3:].strip(" -.:").strip()
                logger.info(f"LLM decided to stop: {reason}")
            return stop
        except Exception as e:
            logger.warning(f"Stop decision failed: {e}")
            return False
    
    # ------------------------------------------------------------------
    # FINAL REPORT
    # ------------------------------------------------------------------
    async def _final_report(self, question: str) -> str:
        """Generate final comprehensive report."""
        claims_view = self.claims_db.get_claims_for_synthesis()
        
        # Add category-specific formatting instructions
        category_instruction = ""
        if self.category and self.category in CATEGORY_PROMPTS:
            category_instruction = "\n\n" + CATEGORY_PROMPTS[self.category]
        
        prompt = FINAL_REPORT_PROMPT.format(
            question=question,
            claims_database=claims_view[:16000],
        ) + category_instruction
        
        try:
            response = await self._llm(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=self.max_report_tokens,
                timeout=120,
            )
            return strip_thinking(response)
        except Exception as e:
            logger.error(f"Final report generation failed: {e}")
            # Fallback: return synthesis
            return self.evolving_report if self.evolving_report else "No report could be generated."
    
    # ------------------------------------------------------------------
    # STATS
    # ------------------------------------------------------------------
    def get_stats(self) -> dict:
        """Return research statistics."""
        elapsed = time.time() - self._start_time if self._start_time else 0
        stats = self.claims_db.get_stats()
        stats.update({
            "rounds_completed": self.round_count,
            "total_queries": len(self.queries_used),
            "total_urls": len(self.urls_fetched),
            "providers_used": list(set(self.providers_used)),
            "elapsed_seconds": round(elapsed, 1),
        })
        return stats
    
    # ------------------------------------------------------------------
    # MAIN RESEARCH LOOP
    # ------------------------------------------------------------------
    async def research(
        self,
        question: str,
        prior_report: str = "",
        prior_findings: Optional[List[Dict]] = None,
        prior_urls: Optional[Set[str]] = None,
    ) -> str:
        """Run iterative research loop."""
        self._start_time = time.time()
        
        if prior_urls:
            self.urls_fetched.update(prior_urls)
        
        consecutive_empty_rounds = 0
        
        # PLAN
        self._emit(phase="planning")
        self.research_plan = await self._create_plan(question)
        logger.info(f"Research plan: {self.research_plan[:200]}")
        
        # CLASSIFY
        if not self.category and not prior_report:
            self.category = await self._classify_category(question)
            if self.category:
                logger.info(f"Auto-detected category: {self.category}")
        
        # ITERATE
        for round_num in range(1, self.max_rounds + 1):
            self.round_count = round_num
            
            # Cancellation check
            if self._cancelled:
                logger.info(f"Cancelled after {round_num - 1} rounds")
                break
            
            # Timeout check
            if self._time_exceeded():
                logger.info(f"Timeout after {round_num - 1} rounds")
                break
            
            logger.info(f"=== Research Round {round_num} ===")
            self._emit(phase="searching", round=round_num, total_sources=len(self.urls_fetched))
            
            # THINK: Generate queries
            queries = await self._generate_queries(question, round_num)
            if not queries:
                logger.warning(f"Round {round_num}: no queries generated")
                break
            
            self._emit(
                phase="searching",
                round=round_num,
                queries=len(queries),
                query_preview=queries[0],
                total_sources=len(self.urls_fetched),
            )
            
            # SEARCH + EXTRACT
            round_findings = await self._search_and_extract(queries, question)
            
            if round_findings:
                self.findings.extend(round_findings)
                consecutive_empty_rounds = 0
                logger.info(f"Round {round_num}: {len(round_findings)} findings")
                self._emit(
                    phase="reading",
                    round=round_num,
                    new_sources=len(round_findings),
                    total_sources=len(self.urls_fetched),
                    total_claims=len(self.claims_db.claims),
                )
            else:
                consecutive_empty_rounds += 1
                logger.info(f"Round {round_num}: no findings ({consecutive_empty_rounds} empty)")
                if consecutive_empty_rounds >= self.max_empty_rounds:
                    logger.warning(f"Search appears down after {self.max_empty_rounds} empty rounds")
                    if not self.findings:
                        return "**Search unavailable** -- all search providers failed."
                    break
            
            # SYNTHESIZE
            if self.claims_db.claims:
                self._emit(phase="analyzing", round=round_num, total_claims=len(self.claims_db.claims))
                self.evolving_report = await self._synthesize(question)
            
            # DECIDE
            if round_num >= self.min_rounds:
                should_stop = await self._should_stop(question, round_num)
                if should_stop:
                    logger.info(f"LLM decided to stop after round {round_num}")
                    break
        
        # FINAL REPORT
        self._emit(phase="writing", total_sources=len(self.urls_fetched), total_claims=len(self.claims_db.claims))
        
        if not self.claims_db.claims:
            return "No information could be gathered."
        
        final = await self._final_report(question)
        elapsed = time.time() - self._start_time
        
        logger.info(
            f"Research complete: {self.round_count} rounds, "
            f"{len(self.findings)} findings, {len(self.claims_db.claims)} claims, "
            f"{len(self.urls_fetched)} URLs, {elapsed:.1f}s"
        )
        
        return final
