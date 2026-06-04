# src/claims_database.py
"""
Structured claims database for deep research v2.
Accumulates atomic facts with dedup, corroboration, contradiction detection.
"""
import logging
import re
import time
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

DEDUP_THRESHOLD = 0.65


class Claim:
    """A single atomic fact extracted from research."""

    __slots__ = ("claim_id", "text", "evidence", "source_url", "source_title",
                 "confidence", "date", "sub_question", "round_found",
                 "corroborations", "contradictions", "created_at")

    def __init__(self, text: str, evidence: str = "", source_url: str = "",
                 source_title: str = "", confidence: float = 0.7,
                 date: str = "", sub_question: str = "", round_found: int = 0):
        self.claim_id = hash(text.strip().lower()) & 0xFFFFFFFF
        self.text = text.strip()
        self.evidence = evidence.strip()[:3000]
        self.source_url = source_url
        self.source_title = source_title or source_url
        self.confidence = min(1.0, max(0.0, confidence))
        self.date = date
        self.sub_question = sub_question
        self.round_found = round_found
        self.corroborations: List[str] = []
        self.contradictions: List[str] = []
        self.created_at = time.time()

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "evidence": self.evidence,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "confidence": round(self.confidence, 2),
            "date": self.date,
            "sub_question": self.sub_question,
            "round_found": self.round_found,
            "corroborations": self.corroborations,
            "contradictions": self.contradictions,
        }


class ClaimsDatabase:
    """Accumulates claims with dedup, corroboration, contradiction detection."""

    def __init__(self):
        self.claims: List[Claim] = []
        self._url_to_claims: Dict[str, List[int]] = {}
        self._claim_index: Dict[int, int] = {}

    def add_claims(self, new_claims: List[dict], round_num: int = 0,
                   sub_question: str = "") -> tuple:
        merged = 0
        added = 0
        for raw in new_claims:
            text = (raw.get("claim") or raw.get("text") or "").strip()
            if not text or len(text) < 10:
                continue
            claim = Claim(
                text=text,
                evidence=raw.get("evidence", ""),
                source_url=raw.get("source_url", raw.get("url", "")),
                source_title=raw.get("source_title", raw.get("title", "")),
                confidence=float(raw.get("confidence", 0.7)),
                date=raw.get("date", ""),
                sub_question=sub_question,
                round_found=round_num,
            )
            existing_idx = self._find_similar(claim)
            if existing_idx is not None:
                existing = self.claims[existing_idx]
                if claim.source_url and claim.source_url not in existing.corroborations:
                    existing.corroborations.append(claim.source_url)
                existing.confidence = min(1.0, existing.confidence + 0.1)
                if len(claim.evidence) > len(existing.evidence):
                    existing.evidence = claim.evidence
                merged += 1
            else:
                idx = len(self.claims)
                self.claims.append(claim)
                self._claim_index[claim.claim_id] = idx
                if claim.source_url:
                    self._url_to_claims.setdefault(claim.source_url, []).append(idx)
                added += 1
        if added or merged:
            self._detect_contradictions()
        return merged, added

    def _find_similar(self, claim: Claim) -> Optional[int]:
        best_score = 0.0
        best_idx = None
        text_lower = claim.text.lower()
        for idx, existing in enumerate(self.claims):
            len_ratio = min(len(text_lower), len(existing.text.lower())) / max(len(text_lower), len(existing.text.lower()), 1)
            if len_ratio < 0.3:
                continue
            score = SequenceMatcher(None, text_lower, existing.text.lower()).ratio()
            if score > best_score:
                best_score = score
                best_idx = idx
        return best_idx if best_score >= DEDUP_THRESHOLD else None

    def _detect_contradictions(self):
        numeric_claims = []
        for idx, c in enumerate(self.claims):
            nums = re.findall(r'[\d,]+\.?\d*', c.text)
            if nums:
                numeric_claims.append((idx, nums))
        for i, (idx_a, nums_a) in enumerate(numeric_claims):
            for j, (idx_b, nums_b) in enumerate(numeric_claims):
                if i >= j:
                    continue
                a = self.claims[idx_a]
                b = self.claims[idx_b]
                a_sans = re.sub(r'[\d,]+\.?\d*', 'NUM', a.text.lower())
                b_sans = re.sub(r'[\d,]+\.?\d*', 'NUM', b.text.lower())
                if SequenceMatcher(None, a_sans, b_sans).ratio() > 0.7:
                    if set(nums_a) != set(nums_b):
                        if str(b.claim_id) not in a.contradictions:
                            a.contradictions.append(str(b.claim_id))
                            b.contradictions.append(str(a.claim_id))

    def get_all_claims(self) -> List[dict]:
        return [c.to_dict() for c in self.claims]

    def get_claims_for_synthesis(self, sub_question: str = "") -> str:
        if not self.claims:
            return "(No claims collected yet.)"
        lines = []
        groups: Dict[str, List[Claim]] = {}
        for c in self.claims:
            key = c.sub_question or "General"
            groups.setdefault(key, []).append(c)
        for sq, claims in groups.items():
            if sub_question and sq != sub_question and sq != "General":
                continue
            lines.append(f"\n### {sq}")
            for c in sorted(claims, key=lambda x: -x.confidence):
                conf_label = "high" if c.confidence >= 0.8 else "medium" if c.confidence >= 0.5 else "low"
                source = f" ([{c.source_title}]({c.source_url}))" if c.source_url else ""
                corrob = ""
                if c.corroborations:
                    corrob = f" [confirmed by {len(c.corroborations)} other source(s)]"
                contra = ""
                if c.contradictions:
                    contra = " [CONTRADICTED by another source]"
                lines.append(f"- **{c.text}**{source}{corrob}{contra}  (confidence: {conf_label})")
                if c.evidence:
                    lines.append(f"  > {c.evidence[:500]}")
        return "\n".join(lines)

    def get_claims_by_sub_question(self, sub_question: str) -> List[Claim]:
        return [c for c in self.claims if c.sub_question == sub_question or c.sub_question == "General"]

    def get_confidence_for_sub_question(self, sub_question: str) -> float:
        relevant = self.get_claims_by_sub_question(sub_question)
        if not relevant:
            return 0.0
        return sum(c.confidence for c in relevant) / len(relevant)

    def get_source_urls(self) -> Set[str]:
        urls = set()
        for c in self.claims:
            if c.source_url:
                urls.add(c.source_url)
            urls.update(c.corroborations)
        return urls

    def get_source_diversity(self) -> Dict[str, int]:
        from urllib.parse import urlparse
        domains: Dict[str, int] = {}
        for c in self.claims:
            if c.source_url:
                try:
                    domain = urlparse(c.source_url).netloc
                    domains[domain] = domains.get(domain, 0) + 1
                except Exception:
                    pass
        return domains

    def get_findings_format(self) -> List[Dict]:
        findings = []
        seen_urls = set()
        for c in self.claims:
            if c.source_url and c.source_url not in seen_urls:
                seen_urls.add(c.source_url)
                findings.append({
                    "url": c.source_url,
                    "title": c.source_title or c.source_url,
                    "summary": c.evidence[:2000] if c.evidence else c.text,
                    "evidence": c.evidence,
                })
        return findings

    def get_stats(self) -> dict:
        total = len(self.claims)
        if total == 0:
            return {"total_claims": 0, "total_sources": 0, "contradictions": 0}
        high = sum(1 for c in self.claims if c.confidence >= 0.8)
        medium = sum(1 for c in self.claims if 0.5 <= c.confidence < 0.8)
        low = sum(1 for c in self.claims if c.confidence < 0.5)
        source_urls = self.get_source_urls()
        contradicted = sum(1 for c in self.claims if c.contradictions)
        domains = self.get_source_diversity()
        sub_qs = set(c.sub_question for c in self.claims if c.sub_question)
        return {
            "total_claims": total,
            "high_confidence": high,
            "medium_confidence": medium,
            "low_confidence": low,
            "total_sources": len(source_urls),
            "unique_domains": len(domains),
            "contradictions": contradicted,
            "sub_questions_covered": len(sub_qs),
            "domains": domains,
        }
