"""
AI Firewall — Pydantic output contracts for every LLM subagent.

Each model maps 1-to-1 to the JSON schema documented in the corresponding
AGENTS.md and system prompt.  Passing one of these classes as ``response_model``
to ``llm_chat()`` activates strict validation: if the LLM returns a payload
that is missing a required field or contains a value outside the declared enum
the call raises ``ValueError`` and tenacity retries the generation, acting as
a prompt-injection / hallucination firewall.

Usage::

    from src.core.contracts import SecurityReport
    result = await llm_chat(messages, response_model=SecurityReport)
    # result is a SecurityReport instance, fully typed and validated.
"""

from typing import Literal

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """
    A single explainable finding or suggestion produced by a subagent.

    Every finding must carry enough information for a human reviewer to locate
    the issue in the codebase, understand its nature, and judge how confident
    the model was when raising it.

    Fields
    ------
    description      : Human-readable explanation of the finding or suggestion.
    file_reference   : Path to the file where the issue was observed
                       (e.g. ``"src/auth/login.py"``).  Use ``""`` when the
                       finding is not tied to a specific file.
    line_number      : Line number within *file_reference* (1-based).  Use
                       ``0`` when no specific line can be identified.
    confidence_score : Model confidence in this finding, in the range
                       ``[0.0, 1.0]``.  Values closer to 1.0 indicate higher
                       certainty; values below 0.5 should be treated as hints.
    """

    description: str
    file_reference: str
    line_number: int = Field(ge=0)
    confidence_score: float = Field(ge=0.0, le=1.0)


class SecurityReport(BaseModel):
    """
    Output contract for the Security subagent.

    Fields
    ------
    status           : Overall verdict — ``"pass"`` or ``"fail"``.
    severity_level   : Highest severity observed in the diff.
    findings_summary : Structured list of concrete findings (empty on pass).
                       Each entry is a :class:`Finding` with description,
                       file_reference, line_number, and confidence_score.
    """

    status: Literal["pass", "fail"]
    severity_level: Literal["low", "medium", "high", "critical"]
    findings_summary: list[Finding] = Field(default_factory=list)


class ArchitectureReport(BaseModel):
    """
    Output contract for the Architecture subagent.

    Fields
    ------
    status               : Verdict — ``"pass"``, ``"warn"``, or ``"fail"``.
    complexity_score     : 1–10 rating of structural complexity (10 = worst).
    refactor_suggestions : Structured list of actionable refactoring notes
                           (empty on pass).  Each entry is a :class:`Finding`
                           with description, file_reference, line_number, and
                           confidence_score.
    """

    status: Literal["pass", "warn", "fail"]
    complexity_score: int = Field(ge=1, le=10)
    refactor_suggestions: list[Finding] = Field(default_factory=list)


class TestingReport(BaseModel):
    """
    Output contract for the Testing subagent.

    Fields
    ------
    status                 : Verdict — ``"pass"`` or ``"warn"``.
    test_coverage_adequate : True when existing tests are deemed sufficient.
    qa_notes               : Structured list of specific observations (empty
                             when coverage is fine).  Each entry is a
                             :class:`Finding` with description, file_reference,
                             line_number, and confidence_score.
    """

    status: Literal["pass", "warn"]
    test_coverage_adequate: bool
    qa_notes: list[Finding] = Field(default_factory=list)


class CriticReport(BaseModel):
    """
    Output contract for the Critic (Lead Reviewer) subagent.

    ``approved_findings`` uses the same :class:`Finding` schema as the
    upstream subagent reports (description, file_reference, line_number,
    confidence_score) so that the Pydantic firewall validates the Critic
    output against the same contract the prompt instructs it to emit.

    Fields
    ------
    overall_status         : Aggregated verdict across all subagents.
    approved_findings      : Findings that survived the zero-noise veto pass.
                             Each :class:`Finding` carries a
                             ``confidence_score`` for downstream explainability.
    vetoed_count           : Number of upstream findings removed as noise /
                             hallucinations.
    summary                : One-paragraph prose summary for the PR author.
    mean_confidence_score  : Arithmetic mean of ``confidence_score`` across all
                             ``approved_findings``.  ``None`` when there are no
                             approved findings.
    """

    overall_status: Literal["pass", "warn", "fail"]
    approved_findings: list[Finding] = Field(default_factory=list)
    vetoed_count: int = Field(ge=0)
    summary: str
    mean_confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
