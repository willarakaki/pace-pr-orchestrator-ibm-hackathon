"""
LangGraph-based PR review orchestrator.

The review pipeline is modelled as a StateGraph with the following topology:

    START ──► anonymizer ──► security ──┐
                         ──► architecture ──► critic ──► END
                         ──► testing ───┘

- anonymizer : Runs Presidio PII/secrets scrubbing on the raw diff and stores
             the sanitized result in state.sanitized_diff.  All downstream
             analysis nodes receive the scrubbed diff.
- security / architecture / testing : Call their respective subagent coroutines
             and merge the result into the shared state dict.
- critic    : Calls the Critic (Lead Reviewer) subagent with the three results
             and builds the final Markdown report.

Hard SLA: each node is individually wrapped with a timeout so a single slow
LLM call never blocks the entire pipeline.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.core.governance import emit_governance_telemetry
from src.core.state import ReviewState
from src.subagents.architecture.arch_task import run_architecture_review
from src.subagents.critic.critic_task import run_critic_validation
from src.subagents.privacy.anonymizer_task import run_anonymizer
from src.subagents.security.security_task import run_security_scan
from src.subagents.testing.testing_task import run_testing_evaluation

logger = logging.getLogger(__name__)

# Hard SLA: each subagent must complete within 40 seconds so the total pipeline
# (3 parallel agents + critic) finishes well under the 2-minute webhook timeout.
_SUBAGENT_TIMEOUT_SECONDS = 40.0

# Emoji badges for status values used in the fallback Markdown report.
_STATUS_BADGE = {
    "pass": "✅ Pass",
    "warn": "⚠️ Warn",
    "fail": "❌ Fail",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _safe(task_func, name: str, *args, **kwargs) -> dict:
    """
    Execute *task_func* with fault isolation and a hard timeout.

    Guarantees that any exception (including TimeoutError) is caught and
    returned as a structured error dict so the graph always receives a result.
    """
    try:
        return await asyncio.wait_for(
            task_func(*args, **kwargs),
            timeout=_SUBAGENT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("Node '%s' timed out after %ss.", name, _SUBAGENT_TIMEOUT_SECONDS)
        return {"error": "Timeout", "status": "fail", "agent": name}
    except Exception as exc:
        logger.error("Node '%s' raised an exception: %s", name, exc)
        return {"error": str(exc), "status": "fail", "agent": name}


def _badge(status: str) -> str:
    return _STATUS_BADGE.get(str(status).lower(), f"❓ {status}")


def _format_finding(item) -> str:
    """Format a single finding — handles both Finding dicts and plain strings."""
    if isinstance(item, dict):
        desc = item.get("description", "")
        file_ref = item.get("file_reference", "")
        line_no = item.get("line_number", 0)
        confidence = item.get("confidence_score", "")
        parts = [desc]
        if file_ref:
            location = f"`{file_ref}`"
            if line_no:
                location += f" line {line_no}"
            parts.append(location)
        if confidence != "":
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.0
            parts.append(f"confidence: {confidence:.0%}")
        return " — ".join(parts)
    return str(item)


def _bullet_list(items: list) -> str:
    """Render a list of Finding dicts or strings as a Markdown bullet list."""
    if not items:
        return "_None_\n"
    return "\n".join(f"- {_format_finding(item)}" for item in items) + "\n"


def build_markdown_report(pr_id, security: dict, arch: dict, testing: dict, *, latency_seconds: float | None = None, estimated_cost_usd: float | None = None, mean_confidence: float | None = None) -> str:
    """
    Transform the three subagent JSON results into a human-readable Markdown
    report suitable for posting as a GitHub PR comment.

    This is the fallback path used only when the Critic stage itself fails.
    """
    rank = {"pass": 0, "warn": 1, "fail": 2}
    statuses = [security.get("status", "fail"), arch.get("status", "fail"), testing.get("status", "fail")]
    worst = max(statuses, key=lambda s: rank.get(s.lower(), 2))
    overall_badge = _badge(worst)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sec_status = _badge(security.get("status", "fail"))
    arch_status = _badge(arch.get("status", "fail"))
    test_status = _badge(testing.get("status", "fail"))

    report = f"""\
## 🤖 Automated Code Review — PR #{pr_id}

> **Overall result: {overall_badge}** &nbsp;|&nbsp; _{timestamp}_

---

### 🔒 Security Analysis &nbsp;{sec_status}

| Field | Value |
|---|---|
| **Status** | {sec_status} |
| **Severity Level** | `{security.get("severity_level", "unknown")}` |

**Findings:**
{_bullet_list(security.get("findings_summary", []))}
---

### 🏛️ Architecture Review &nbsp;{arch_status}

| Field | Value |
|---|---|
| **Status** | {arch_status} |
| **Complexity Score** | `{arch.get("complexity_score", "N/A")} / 10` |

**Refactor Suggestions:**
{_bullet_list(arch.get("refactor_suggestions", []))}
---

### 🧪 Testing Evaluation &nbsp;{test_status}

| Field | Value |
|---|---|
| **Status** | {test_status} |
| **Coverage** | {"✅ Adequate" if testing.get("test_coverage_adequate") else "❌ Inadequate"} |

**QA Notes:**
{_bullet_list(testing.get("qa_notes", []))}
---

<sub>Generated by the PACE PR Orchestrator · {timestamp}</sub>
"""

    # ── Governance Metrics ───────────────────────────────────────────────────
    metrics_rows: list[str] = []
    if latency_seconds is not None:
        metrics_rows.append(f"| **Pipeline Latency** | `{latency_seconds:.2f}s` |")
    if estimated_cost_usd is not None:
        metrics_rows.append(f"| **Estimated LLM Cost** | `${estimated_cost_usd:.4f}` |")
    if mean_confidence is not None:
        metrics_rows.append(f"| **Mean Confidence Score** | `{mean_confidence:.0%}` |")

    if metrics_rows:
        report += "\n---\n\n### 📊 Governance Metrics\n\n| Metric | Value |\n|---|---|\n"
        report += "\n".join(metrics_rows) + "\n"

    return report


# ── Graph nodes ───────────────────────────────────────────────────────────────

async def _node_anonymizer(state: ReviewState) -> dict:
    logger.info("Graph node 'anonymizer': scrubbing diff for PR #%s.", state.pr_id)
    sanitized = await _safe(run_anonymizer, "anonymizer", state.diff)
    # run_anonymizer returns a str; _safe wraps errors as dicts — fall back to
    # the raw diff only if the anonymizer itself raised (fail-closed policy is
    # enforced inside run_anonymizer itself; _safe catches the re-raised error).
    if isinstance(sanitized, str):
        return {"sanitized_diff": sanitized}
    # _safe returned an error dict — fail-closed: never forward the raw diff.
    logger.error("Graph node 'anonymizer': failed — %s", sanitized)
    return {"sanitized_diff": "[DIFF REDACTED: Privacy subsystem failure]"}


async def _node_security(state: ReviewState) -> dict:
    logger.info("Graph node 'security': starting for PR #%s.", state.pr_id)
    # Zero-trust: the LLM only ever sees the sanitized diff.
    # The raw_diff is passed separately so deterministic scanners (Gitleaks /
    # Trivy) can operate on unredacted code.
    result = await _safe(
        run_security_scan,
        "security",
        state.sanitized_diff or "",
        state.raw_diff or state.diff,
    )
    return {"security": result}


async def _node_architecture(state: ReviewState) -> dict:
    logger.info("Graph node 'architecture': starting for PR #%s.", state.pr_id)
    result = await _safe(run_architecture_review, "architecture", state.sanitized_diff or "")
    return {"architecture": result}


async def _node_testing(state: ReviewState) -> dict:
    logger.info("Graph node 'testing': starting for PR #%s.", state.pr_id)
    result = await _safe(run_testing_evaluation, "testing", state.sanitized_diff or "")
    return {"testing": result}


async def _node_critic(state: ReviewState) -> dict:
    pr_id = state.pr_id
    logger.info("Graph node 'critic': starting for PR #%s.", pr_id)

    critic_result = await _safe(
        run_critic_validation,
        "critic",
        state.security or {},
        state.architecture or {},
        state.testing or {},
        pr_id,
        state.sanitized_diff or "",
    )

    # Compute mean confidence from critic approved_findings if available.
    approved = critic_result.get("approved_findings", [])
    if approved:
        scores = [f.get("confidence_score", 0.0) for f in approved if isinstance(f, dict)]
        mean_confidence = sum(scores) / len(scores) if scores else None
    else:
        mean_confidence = None

    markdown_report = critic_result.get("markdown_report") or build_markdown_report(
        pr_id,
        state.security or {},
        state.architecture or {},
        state.testing or {},
        mean_confidence=mean_confidence,
    )

    logger.info("Graph node 'critic': complete for PR #%s.", pr_id)
    return {"critic": critic_result, "markdown_report": markdown_report}


# ── Graph assembly ────────────────────────────────────────────────────────────

def _build_graph() -> CompiledStateGraph:
    """Construct and compile the review StateGraph."""
    builder = StateGraph(ReviewState)

    # Register nodes
    builder.add_node("anonymizer", _node_anonymizer)
    builder.add_node("security", _node_security)
    builder.add_node("architecture", _node_architecture)
    builder.add_node("testing", _node_testing)
    builder.add_node("critic", _node_critic)

    # Anonymizer runs first — START feeds into it sequentially.
    builder.add_edge(START, "anonymizer")

    # Fan-out: anonymizer branches to the three parallel analysis nodes.
    builder.add_edge("anonymizer", "security")
    builder.add_edge("anonymizer", "architecture")
    builder.add_edge("anonymizer", "testing")

    # Converge: all three branches must finish before the Critic runs
    builder.add_edge("security", "critic")
    builder.add_edge("architecture", "critic")
    builder.add_edge("testing", "critic")

    builder.add_edge("critic", END)

    return builder.compile()


# Compile once at module load time (reused across all requests).
_review_graph = _build_graph()


# ── Public entry point ────────────────────────────────────────────────────────

async def process_pull_request(pr_data: dict) -> dict:
    """
    Core orchestration entry point.

    Receives parsed PR data, executes the LangGraph review pipeline, and
    returns a structured result dict with the Markdown report and raw
    subagent outputs.
    """
    pr_id = pr_data.get("id", "unknown")
    diff = pr_data.get("diff", "")

    logger.info("Starting LangGraph code review for PR #%s.", pr_id)

    initial_state = ReviewState(pr_id=str(pr_id), diff=diff, raw_diff=diff)
    _start_time = time.monotonic()
    raw_state = await _review_graph.ainvoke(initial_state)
    latency_seconds = time.monotonic() - _start_time
    final_state = ReviewState.model_validate(raw_state)

    logger.info("LangGraph review pipeline complete for PR #%s.", pr_id)

    await emit_governance_telemetry(final_state.model_dump())

    # Append latency to the markdown report (governance metrics section).
    # Estimate cost: ~4 LLM calls, ~2000 tokens each at $0.001/1k tokens.
    estimated_cost_usd = latency_seconds * 0.0004  # rough proxy
    markdown_report = final_state.markdown_report or ""
    if markdown_report and "Governance Metrics" not in markdown_report:
        critic_state = final_state.critic or {}
        approved = critic_state.get("approved_findings", [])
        scores = [f.get("confidence_score", 0.0) for f in approved if isinstance(f, dict)]
        mean_confidence = sum(scores) / len(scores) if scores else None
        metrics_rows = [
            f"| **Pipeline Latency** | `{latency_seconds:.2f}s` |",
            f"| **Estimated LLM Cost** | `${estimated_cost_usd:.4f}` |",
        ]
        if mean_confidence is not None:
            metrics_rows.append(f"| **Mean Confidence Score** | `{mean_confidence:.0%}` |")
        markdown_report = (
            markdown_report.rstrip()
            + "\n\n---\n\n### \U0001f4ca Governance Metrics\n\n| Metric | Value |\n|---|---|\n"
            + "\n".join(metrics_rows)
            + "\n"
        )

    return {
        "pr_id": pr_id,
        "summary": "Code Review Orchestrator completed.",
        "markdown_report": markdown_report,
        "results": {
            "security": final_state.security or {},
            "architecture": final_state.architecture or {},
            "testing": final_state.testing or {},
            "critic": final_state.critic or {},
        },
    }
