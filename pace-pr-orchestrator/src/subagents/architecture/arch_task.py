import logging

from src.core.contracts import ArchitectureReport
from src.core.llm_client import llm_chat
from src.prompts.loader import load_prompt

logger = logging.getLogger(__name__)

# PRs exceeding this line count receive a mandatory "High Risk" warning regardless
# of what the LLM returns, as a deterministic safety net.
_LARGE_PR_THRESHOLD = 400


async def run_architecture_review(diff: str) -> dict:
    """
    Architecture Subagent — detects structural degradation and code bloat.

    Calls the LLM factory with the Git diff and parses the structured JSON
    response.  Falls back to a safe error dict if the call fails.  The
    400-line threshold rule is applied deterministically on top of the LLM
    result.

    Required output schema (per AGENTS.md contract):
        status              : "pass" | "warn" | "fail"
        complexity_score    : int  (1–10)
        refactor_suggestions: list[str]
    """
    logger.info("Architecture subagent: starting review.")

    system_prompt = load_prompt("architecture_prompt.md")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"<UNTRUSTED_PAYLOAD>\n{diff}\n</UNTRUSTED_PAYLOAD>"},
    ]

    try:
        report = await llm_chat(messages, response_model=ArchitectureReport)
        logger.info("Architecture subagent: review complete. Status: %s", report.status)
        result = report.model_dump()
    except Exception as e:
        logger.error("Architecture subagent: LLM call failed — %s", e)
        # Fallback must use the Finding schema (description/file_reference/
        # line_number/confidence_score) so downstream Pydantic validation
        # does not crash on a flat list of strings.
        result = {
            "status": "fail",
            "complexity_score": 1,
            "refactor_suggestions": [
                {
                    "description": f"Architecture review failed: {e}",
                    "file_reference": "N/A",
                    "line_number": 0,
                    "confidence_score": 1.0,
                }
            ],
        }

    # Deterministic large-PR safety net applied regardless of LLM outcome.
    line_count = len(diff.splitlines())
    if line_count > _LARGE_PR_THRESHOLD:
        if result.get("status") == "pass":
            result["status"] = "warn"
        suggestions = result.setdefault("refactor_suggestions", [])
        large_pr_note = {
            "description": (
                "High Risk: Large PRs severely degrade human review quality. "
                "Consider splitting this PR into smaller, focused changes."
            ),
            "file_reference": "N/A",
            "line_number": 0,
            "confidence_score": 1.0,
        }
        if large_pr_note not in suggestions:
            suggestions.append(large_pr_note)

    return result
