"""
Shared LangGraph state definition.

Centralising ReviewState here prevents circular imports: the orchestrator,
every subagent node, and any future utility can all import from this single
module without creating dependency cycles.
"""

from typing import Optional

from pydantic import BaseModel, Field


class ReviewState(BaseModel):
    """
    Shared mutable state threaded through every node in the review graph.

    Fields
    ------
    pr_id           : Pull request identifier (string form of the PR number).
    diff            : Raw diff string as received from the webhook payload.
    raw_diff        : Preserved copy of the original diff, used exclusively by
                    deterministic scanners (Gitleaks / Trivy) that must operate
                    on unredacted code.  Never forwarded to the LLM.
    sanitized_diff  : PII/secret-scrubbed diff produced by the anonymizer node.
                    All downstream analysis nodes consume this field instead
                    of ``diff`` so no sensitive data ever reaches the LLM.
    security        : Structured result dict from the Security subagent.
    architecture    : Structured result dict from the Architecture subagent.
    testing         : Structured result dict from the Testing subagent.
    critic          : Structured result dict from the Critic subagent.
    markdown_report : Final rendered Markdown string for the GitHub PR comment.
    """

    pr_id: str = Field(...)
    diff: str = Field(...)
    raw_diff: str = Field(default="")
    sanitized_diff: Optional[str] = Field(default=None)
    security: Optional[dict] = Field(default=None)
    architecture: Optional[dict] = Field(default=None)
    testing: Optional[dict] = Field(default=None)
    critic: Optional[dict] = Field(default=None)
    markdown_report: Optional[str] = Field(default=None)
