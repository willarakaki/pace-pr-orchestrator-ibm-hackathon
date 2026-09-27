"""
tests/test_orchestrator.py
──────────────────────────────────────────────────────────────────────────────
Integration-style unit tests for src/orchestrator.py.

All external I/O (LLM calls, GitHub API) is eliminated by patching the four
subagent coroutines.  Tests exercise the LangGraph wiring, result propagation,
and the Critic fallback path without ever touching a network.

Environment setup
-----------------
LLM_API_URL is required at import time by src/core/config.  We set it once in
the module-level autouse fixture so every test in this file gets a clean env
without polluting the real environment.
"""

import asyncio
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Stub agentops before any src module loads ─────────────────────────────────
# The installed agentops version may not provide `record_action`; stub it so
# the test suite never depends on the agentops SDK being fully present in CI.
if not hasattr(__import__("agentops"), "record_action"):
    import agentops as _agentops_mod
    _agentops_mod.record_action = lambda *a, **kw: (lambda f: f)  # passthrough decorator

# ── Environment prerequisite ──────────────────────────────────────────────────
# config.py validates LLM_API_URL at import time; set it before the module tree
# loads so the import doesn't raise RuntimeError in CI.
import os
os.environ.setdefault("LLM_API_URL", "https://dummy.local/v1")

from src.orchestrator import build_markdown_report, process_pull_request  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_DIFF = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,6 @@
+import os
+SECRET = os.environ.get("DB_PASSWORD")
 def main():
     pass
"""

SECURITY_RESULT = {
    "status": "warn",
    "severity_level": "medium",
    "findings_summary": ["Potential secret exposure via env var"],
}

ARCH_RESULT = {
    "status": "pass",
    "complexity_score": 3,
    "refactor_suggestions": [],
}

TESTING_RESULT = {
    "status": "fail",
    "test_coverage_adequate": False,
    "qa_notes": ["No tests cover the new code path"],
}

CRITIC_RESULT = {
    "status": "fail",
    "markdown_report": "## 🤖 Critic says: needs work",
}


@pytest.fixture()
def mock_subagents():
    """
    Patch all four subagent coroutines and the anonymizer with deterministic
    AsyncMock returns.  The anonymizer is mocked to prevent Presidio from
    being invoked during CI (no ML models required).
    The fixture yields the four analysis mocks so individual tests can override them.
    """
    with (
        patch("src.orchestrator.run_anonymizer",          new_callable=AsyncMock, return_value=SAMPLE_DIFF)           as _mock_anon,  # noqa: F841
        patch("src.orchestrator.run_security_scan",        new_callable=AsyncMock, return_value=SECURITY_RESULT)  as mock_sec,
        patch("src.orchestrator.run_architecture_review",  new_callable=AsyncMock, return_value=ARCH_RESULT)       as mock_arch,
        patch("src.orchestrator.run_testing_evaluation",   new_callable=AsyncMock, return_value=TESTING_RESULT)    as mock_test,
        patch("src.orchestrator.run_critic_validation",    new_callable=AsyncMock, return_value=CRITIC_RESULT)     as mock_critic,
    ):
        yield mock_sec, mock_arch, mock_test, mock_critic


# ── Happy-path: full pipeline ─────────────────────────────────────────────────

class TestProcessPullRequest:

    @pytest.mark.asyncio
    async def test_returns_expected_keys(self, mock_subagents):
        """Result dict must contain all documented top-level keys."""
        result = await process_pull_request({"id": "42", "diff": SAMPLE_DIFF})

        assert "pr_id" in result
        assert "summary" in result
        assert "markdown_report" in result
        assert "results" in result

    @pytest.mark.asyncio
    async def test_pr_id_is_forwarded(self, mock_subagents):
        """pr_id in the result must match the input."""
        result = await process_pull_request({"id": "99", "diff": SAMPLE_DIFF})
        assert result["pr_id"] == "99"

    @pytest.mark.asyncio
    async def test_subagent_results_are_present(self, mock_subagents):
        """All three analysis results must appear under 'results'."""
        result = await process_pull_request({"id": "1", "diff": SAMPLE_DIFF})
        assert result["results"]["security"] == SECURITY_RESULT
        assert result["results"]["architecture"] == ARCH_RESULT
        assert result["results"]["testing"] == TESTING_RESULT

    @pytest.mark.asyncio
    async def test_critic_markdown_used_when_present(self, mock_subagents):
        """When the critic returns a markdown_report it should be the base of the final report."""
        result = await process_pull_request({"id": "7", "diff": SAMPLE_DIFF})
        assert result["markdown_report"].startswith(CRITIC_RESULT["markdown_report"])

    @pytest.mark.asyncio
    async def test_all_subagents_called_exactly_once(self, mock_subagents):
        """Each subagent coroutine must be invoked exactly once per PR."""
        mock_sec, mock_arch, mock_test, mock_critic = mock_subagents
        await process_pull_request({"id": "3", "diff": SAMPLE_DIFF})
        mock_sec.assert_awaited_once()
        mock_arch.assert_awaited_once()
        mock_test.assert_awaited_once()
        mock_critic.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_diff_key_uses_empty_string(self, mock_subagents):
        """A PR payload without 'diff' must not raise — it defaults to ''."""
        result = await process_pull_request({"id": "5"})
        assert result["pr_id"] == "5"


# ── Fault isolation: subagent failures ───────────────────────────────────────

class TestSubagentFaultIsolation:

    @pytest.mark.asyncio
    async def test_security_subagent_exception_is_isolated(self):
        """An exception in security must not crash the pipeline."""
        with (
            patch("src.orchestrator.run_anonymizer",          new_callable=AsyncMock, return_value=SAMPLE_DIFF),
            patch("src.orchestrator.run_security_scan",       new_callable=AsyncMock, side_effect=RuntimeError("LLM unavailable")),
            patch("src.orchestrator.run_architecture_review", new_callable=AsyncMock, return_value=ARCH_RESULT),
            patch("src.orchestrator.run_testing_evaluation",  new_callable=AsyncMock, return_value=TESTING_RESULT),
            patch("src.orchestrator.run_critic_validation",   new_callable=AsyncMock, return_value=CRITIC_RESULT),
        ):
            result = await process_pull_request({"id": "err-1", "diff": SAMPLE_DIFF})
            assert result["results"]["security"].get("status") == "fail"
            assert "error" in result["results"]["security"]

    @pytest.mark.asyncio
    async def test_critic_failure_falls_back_to_built_in_report(self):
        """When the critic fails the pipeline must still produce a markdown_report."""
        with (
            patch("src.orchestrator.run_anonymizer",          new_callable=AsyncMock, return_value=SAMPLE_DIFF),
            patch("src.orchestrator.run_security_scan",       new_callable=AsyncMock, return_value=SECURITY_RESULT),
            patch("src.orchestrator.run_architecture_review", new_callable=AsyncMock, return_value=ARCH_RESULT),
            patch("src.orchestrator.run_testing_evaluation",  new_callable=AsyncMock, return_value=TESTING_RESULT),
            patch("src.orchestrator.run_critic_validation",   new_callable=AsyncMock, side_effect=Exception("Critic timeout")),
        ):
            result = await process_pull_request({"id": "err-2", "diff": SAMPLE_DIFF})
            # The fallback renderer must produce a non-empty string.
            assert isinstance(result["markdown_report"], str)
            assert len(result["markdown_report"]) > 0


# ── build_markdown_report (pure function) ─────────────────────────────────────

class TestBuildMarkdownReport:
    """Tests for the pure Markdown fallback renderer — no mocks needed."""

    def test_contains_pr_id(self):
        report = build_markdown_report("42", SECURITY_RESULT, ARCH_RESULT, TESTING_RESULT)
        assert "42" in report

    def test_worst_status_is_fail_when_any_fails(self):
        report = build_markdown_report("1", SECURITY_RESULT, ARCH_RESULT, TESTING_RESULT)
        # TESTING_RESULT status is "fail" so overall badge must be Fail.
        assert "❌ Fail" in report

    def test_overall_pass_when_all_pass(self):
        all_pass = {"status": "pass", "severity_level": "none",
                    "findings_summary": [], "complexity_score": 1,
                    "refactor_suggestions": [], "test_coverage_adequate": True,
                    "qa_notes": []}
        report = build_markdown_report("2", all_pass, all_pass, all_pass)
        assert "✅ Pass" in report

    def test_security_section_present(self):
        report = build_markdown_report("3", SECURITY_RESULT, ARCH_RESULT, TESTING_RESULT)
        assert "Security Analysis" in report

    def test_architecture_section_present(self):
        report = build_markdown_report("3", SECURITY_RESULT, ARCH_RESULT, TESTING_RESULT)
        assert "Architecture Review" in report

    def test_testing_section_present(self):
        report = build_markdown_report("3", SECURITY_RESULT, ARCH_RESULT, TESTING_RESULT)
        assert "Testing Evaluation" in report

    def test_findings_are_rendered(self):
        report = build_markdown_report("4", SECURITY_RESULT, ARCH_RESULT, TESTING_RESULT)
        assert "Potential secret exposure via env var" in report

    def test_empty_findings_renders_none_note(self):
        no_findings = {**SECURITY_RESULT, "findings_summary": []}
        report = build_markdown_report("5", no_findings, ARCH_RESULT, TESTING_RESULT)
        assert "_None_" in report
