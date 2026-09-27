import logging

from src.core.contracts import TestingReport
from src.core.llm_client import llm_chat
from src.prompts.loader import load_prompt

logger = logging.getLogger(__name__)


async def run_testing_evaluation(diff: str) -> dict:
    """
    Testing Subagent — evaluates test coverage and detects flaky test patterns.

    Calls the LLM factory with the Git diff and parses the structured JSON
    response.  Falls back to a safe error dict if the call fails.

    Required output schema (per AGENTS.md contract):
        status                 : "pass" | "warn"
        test_coverage_adequate : bool
        qa_notes               : list[str]
    """
    logger.info("Testing subagent: starting evaluation.")

    system_prompt = load_prompt("testing_prompt.md")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"<UNTRUSTED_PAYLOAD>\n{diff}\n</UNTRUSTED_PAYLOAD>"},
    ]

    try:
        report = await llm_chat(messages, response_model=TestingReport)
        logger.info("Testing subagent: evaluation complete. Status: %s", report.status)
        return report.model_dump()
    except Exception as e:
        logger.error("Testing subagent: LLM call failed — %s", e)
        # Fallback must use the Finding schema so downstream Pydantic validation
        # does not crash on a flat list of strings.
        return {
            "status": "warn",
            "test_coverage_adequate": False,
            "qa_notes": [
                {
                    "description": f"Testing evaluation failed: {e}",
                    "file_reference": "N/A",
                    "line_number": 0,
                    "confidence_score": 1.0,
                }
            ],
        }
