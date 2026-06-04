# src/structured_extractor.py
"""
Structured claim extractor for deep research v2.
Extracts atomic facts with evidence, confidence, and date.
"""

STRUCTURED_EXTRACTOR_PROMPT = """\
You are a research analyst extracting atomic facts from web content.

**Research goal:** {goal}

**Webpage content:**
{webpage_content}

**Instructions:**
Extract individual, verifiable claims (atomic facts) from this content that relate to the research goal.

For each claim:
- Write it as a single, specific, factual statement
- Include the exact evidence (quote) from the source
- Rate confidence: high (clearly stated), medium (implied/inferred), low (speculative/uncertain)
- Include publication date if available

Return ONLY a JSON array of claim objects. Example:
[
  {{
    "claim": "Sandalphon is associated with the sefirah of Malkuth in Kabbalah",
    "evidence": "In Kabbalistic tradition, Sandalphon guards the sefirah of Malkuth...",
    "confidence": "high",
    "date": "2024"
  }}
]

If no relevant claims found, return: []
"""


def parse_structured_response(response: str) -> list:
    """Parse the LLM's JSON response into a list of claim dicts."""
    import json
    import re
    
    if not response:
        return []
    
    text = response.strip()
    
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*```$', '', text)
    
    match = re.search(r'\[[\s\S]*\]', text)
    if not match:
        return []
    
    try:
        claims = json.loads(match.group())
        if not isinstance(claims, list):
            return []
        
        normalized = []
        for c in claims:
            if not isinstance(c, dict):
                continue
            claim_text = c.get("claim", "").strip()
            if len(claim_text) < 10:
                continue
            
            conf_str = (c.get("confidence") or "medium").lower()
            conf_map = {"high": 0.9, "medium": 0.7, "low": 0.4}
            confidence = conf_map.get(conf_str, 0.7)
            
            normalized.append({
                "claim": claim_text,
                "evidence": c.get("evidence", "").strip()[:3000],
                "confidence": confidence,
                "date": c.get("date", ""),
            })
        
        return normalized
    
    except (json.JSONDecodeError, ValueError) as e:
        return []
