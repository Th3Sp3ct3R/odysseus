"""
Integration test for DeepResearcherV2.
Tests the full research pipeline with structured extraction and claims database.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.deep_research_v2 import DeepResearcherV2


async def test_sandalphon_research():
    """Test research with SandalPhon prompt.

    Live integration test — hits OpenRouter over the network. Opt-in only so it
    never red-flags the default unit suite: set RUN_RESEARCH_INTEGRATION=1 and a
    valid OPENROUTER_API_KEY to run it.
    """

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not os.environ.get("RUN_RESEARCH_INTEGRATION") or not api_key:
        pytest.skip(
            "live OpenRouter integration test; set RUN_RESEARCH_INTEGRATION=1 "
            "and OPENROUTER_API_KEY to run"
        )

    # Initialize researcher with OpenRouter
    researcher = DeepResearcherV2(
        llm_endpoint="https://openrouter.ai/api/v1/chat/completions",
        llm_model="openai/gpt-4o-mini",  # Fast and cheap for testing
        llm_headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://odysseus.local",
            "X-Title": "Odysseus Research Test",
        },
        max_rounds=5,
        min_rounds=2,
        max_time=120,
        max_urls_per_round=3,
    )

    # SandalPhon prompt
    prompt = (
        "we we want a deep research on the Archangel SandalPhon the Angel of Music "
        "give me Song Titles and Descriptions for Suno as my artist/producer name is Sandalphon.OS and "
        "image prompts that will create cover art using this research from its lore"
    )

    print("\n" + "=" * 80)
    print("STARTING RESEARCH v2 TEST")
    print("=" * 80)
    print(f"Prompt: {prompt[:100]}...")
    print("Config: max_rounds=5, min_rounds=2, max_time=120s")
    print("=" * 80 + "\n")

    # Run research
    report = await researcher.research(prompt)

    # Print results
    print("\n" + "=" * 80)
    print("FINAL REPORT")
    print("=" * 80)
    print(report)

    print("\n" + "=" * 80)
    print("CLAIMS DATABASE STATS")
    print("=" * 80)
    stats = researcher.get_stats()
    print(f"Rounds completed: {stats['rounds_completed']}")
    print(f"Total queries: {stats['total_queries']}")
    print(f"Total URLs fetched: {stats['total_urls']}")
    print(f"Providers used: {stats['providers_used']}")
    print(f"Elapsed time: {stats['elapsed_seconds']}s")
    print(f"\nClaims statistics:")
    print(f"  Total claims: {stats['total_claims']}")
    print(f"  Claims merged (dedup): {stats['claims_merged']}")
    print(f"  Contradictions detected: {stats['contradictions_detected']}")
    print(f"  High confidence: {stats['high_confidence']:.1f}")
    print(f"  Medium confidence: {stats['medium_confidence']:.1f}")
    print(f"  Low confidence: {stats['low_confidence']:.1f}")

    if stats.get('sub_question_coverage'):
        print(f"\nSub-question coverage:")
        for sq, coverage in stats['sub_question_coverage'].items():
            print(f"  {sq[:60]}: {coverage['confidence']:.2f} ({coverage['claims']} claims)")

    print("=" * 80)
    print("\n✅ Test completed successfully!")

    return report


if __name__ == "__main__":
    asyncio.run(test_sandalphon_research())
