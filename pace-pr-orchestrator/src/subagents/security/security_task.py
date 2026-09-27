import asyncio
import logging
import os
import tempfile

from src.core.contracts import SecurityReport
from src.core.llm_client import llm_chat
from src.prompts.loader import load_prompt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deterministic scan helpers
# ---------------------------------------------------------------------------

async def _run_gitleaks(diff: str) -> str | None:
    """
    Write the diff to a temp file and run `gitleaks detect` against it.
    Returns a short findings string on success, or None if the binary is
    unavailable or produces no findings (exit 0 with no output).
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(diff)
            tmp_path = tmp.name

        proc = await asyncio.create_subprocess_exec(
            "gitleaks", "detect", "--no-git", f"--source={tmp_path}", "-f", "json", "--exit-code", "1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode(errors="replace").strip()
        if output:
            return output
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            return f"gitleaks exited {proc.returncode}: {err}" if err else None
        return None
    except FileNotFoundError:
        logger.debug("gitleaks binary not found — skipping deterministic scan.")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("gitleaks scan error — %s", exc)
        return None
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def _run_trivy(diff: str) -> str | None:
    """
    Write the diff to a temp file and run `trivy fs` against it.
    Returns a short findings string on success, or None if the binary is
    unavailable.
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(diff)
            tmp_path = tmp.name

        proc = await asyncio.create_subprocess_exec(
            "trivy", "fs", "--exit-code", "1", "--severity", "HIGH,CRITICAL", "--quiet", tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode(errors="replace").strip()
        return output if output else None
    except FileNotFoundError:
        logger.debug("trivy binary not found — skipping deterministic scan.")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("trivy scan error — %s", exc)
        return None
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def _deterministic_scan(diff: str) -> str:
    """
    Run gitleaks then trivy concurrently.  Returns a formatted summary string
    (empty string when both tools are absent or find nothing).
    """
    gitleaks_out, trivy_out = await asyncio.gather(
        _run_gitleaks(diff),
        _run_trivy(diff),
    )
    parts: list[str] = []
    if gitleaks_out:
        parts.append(f"=== Gitleaks findings ===\n{gitleaks_out}")
    if trivy_out:
        parts.append(f"=== Trivy findings ===\n{trivy_out}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_security_scan(sanitized_diff: str, raw_diff: str = "") -> dict:
    """
    Security Subagent — detects critical and high-severity vulnerabilities.

    1. Runs gitleaks + trivy deterministically against *raw_diff* (the original
       unredacted patch) so no secrets are hidden from the scanners.
    2. Appends the tool output to the LLM prompt, which receives only
       *sanitized_diff* so no sensitive data ever reaches the LLM.

    Required output schema (per AGENTS.md contract):
        status          : "pass" | "fail"
        severity_level  : str
        findings_summary: list[str]
    """
    logger.info("Security subagent: starting scan.")

    # --- Step 1: deterministic scan on raw (unredacted) code ----------------
    # Fall back to sanitized_diff if raw_diff was not supplied.
    scan_target = raw_diff if raw_diff else sanitized_diff
    deterministic_findings = await _deterministic_scan(scan_target)
    if deterministic_findings:
        logger.info("Security subagent: deterministic scan produced findings.")
    else:
        logger.info("Security subagent: deterministic scan found nothing (or tools absent).")

    # --- Step 2: build LLM prompt, injecting deterministic findings ----------
    system_prompt = load_prompt("security_prompt.md")

    # LLM only ever sees the sanitized diff — no raw secrets forwarded.
    # Wrap in UNTRUSTED_PAYLOAD tags to signal adversarial-input handling.
    user_content = f"<UNTRUSTED_PAYLOAD>\n{sanitized_diff}\n</UNTRUSTED_PAYLOAD>"
    if deterministic_findings:
        user_content += (
            "\n\n---\n"
            "The following findings were produced by deterministic security tools "
            "(Gitleaks / Trivy) before you were invoked. "
            "You MUST incorporate these into your final JSON response:\n\n"
            f"{deterministic_findings}"
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    # --- Step 3: LLM call with Pydantic firewall ----------------------------
    try:
        report = await llm_chat(messages, response_model=SecurityReport)
        logger.info("Security subagent: scan complete. Status: %s", report.status)
        return report.model_dump()
    except Exception as e:
        logger.error("Security subagent: LLM call failed — %s", e)
        # Fallback findings must use the Finding schema so downstream Pydantic
        # validation does not crash on flat strings.
        fallback_description = (
            f"LLM call failed; deterministic scan results follow: {deterministic_findings}"
            if deterministic_findings
            else f"Security scan failed: {e}"
        )
        return {
            "status": "fail",
            "severity_level": "high" if deterministic_findings else "unknown",
            "findings_summary": [
                {
                    "description": fallback_description,
                    "file_reference": "N/A",
                    "line_number": 0,
                    "confidence_score": 1.0,
                }
            ],
        }
