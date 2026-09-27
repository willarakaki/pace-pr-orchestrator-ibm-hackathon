"""
Privacy Anonymizer subagent — zero-trust PII/secrets scrubbing node.

This node runs BEFORE the diff reaches any LLM.  It uses Microsoft Presidio
to detect and replace sensitive entities so the downstream Security,
Architecture, and Testing agents never see raw PII or credentials.

Entities scrubbed by default
-----------------------------
- PERSON            — human names
- EMAIL_ADDRESS     — email addresses
- PHONE_NUMBER      — phone numbers
- IP_ADDRESS        — IPv4 and IPv6 addresses
- CREDIT_CARD       — credit-card numbers
- CRYPTO            — cryptocurrency wallet addresses
- IBAN_CODE         — international bank account numbers
- US_SSN            — US social security numbers
- (URL is excluded to prevent misclassifying .py file paths as web URLs)

Presidio models load once at module import time (lazy singleton pattern) to
avoid paying the initialisation cost on every request.

Token Guardrail
---------------
Before Presidio runs, ``_filter_and_truncate_diff`` strips well-known bloated
file types (lock files, minified JS/CSS, SVGs, generated vendored assets) and
applies a hard character cap (``MAX_DIFF_LENGTH``) to prevent context-window
overflows and runaway token costs on massive PRs.
"""

import logging
import re
from functools import lru_cache

from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine

logger = logging.getLogger(__name__)

# ── Token Guardrail ────────────────────────────────────────────────────────────

# Hard character limit applied to the diff before it reaches Presidio or any
# LLM node.  12 000 chars ≈ ~3 000 tokens, comfortably inside typical context
# windows while still covering meaningful code changes.
MAX_DIFF_LENGTH: int = 12_000

# File-path patterns whose diff hunks are unconditionally dropped.  These files
# are almost always generated/vendored and add enormous token bulk with zero
# review value.
_BLOAT_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        r"^diff --git .+ package-lock\.json",
        r"^diff --git .+ yarn\.lock",
        r"^diff --git .+ poetry\.lock",
        r"^diff --git .+ Pipfile\.lock",
        r"^diff --git .+ composer\.lock",
        r"^diff --git .+ Gemfile\.lock",
        r"^diff --git .+ pnpm-lock\.yaml",
        r"^diff --git .+ \.min\.(js|css)",
        r"^diff --git .+ \.bundle\.js",
        r"^diff --git .+ \.svg",
        r"^diff --git .+ \.map",          # source-map files
        r"^diff --git .+ dist/",
        r"^diff --git .+ vendor/",
        r"^diff --git .+ node_modules/",
        r"^diff --git .+ \.pb\.go",       # protobuf generated Go
        r"^diff --git .+ _pb2\.py",       # protobuf generated Python
        r"^diff --git .+ \.generated\.",
    ]
]


def _filter_and_truncate_diff(diff: str) -> str:
    """Strip bloated file hunks and enforce ``MAX_DIFF_LENGTH``.

    The function splits the unified diff on ``diff --git`` boundaries, drops
    any hunk whose header matches a known-bloat pattern, then reassembles the
    remainder.  If the result still exceeds ``MAX_DIFF_LENGTH`` characters it
    is hard-truncated and a sentinel warning is appended so downstream LLMs
    know they are reading a partial diff.

    Args:
        diff: Raw Git diff string.

    Returns:
        Filtered (and possibly truncated) diff string.
    """
    # Split into per-file hunks while preserving the "diff --git" header line.
    # The split keeps the delimiter at the start of each element (lookahead).
    hunks = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)

    kept: list[str] = []
    dropped: int = 0
    for hunk in hunks:
        first_line = hunk.split("\n", 1)[0]
        if any(pat.search(first_line) for pat in _BLOAT_FILE_PATTERNS):
            dropped += 1
            logger.debug("Token guardrail: dropped bloat hunk — %s", first_line)
        else:
            kept.append(hunk)

    if dropped:
        logger.info(
            "Token guardrail: filtered %d bloated file hunk(s) from diff.", dropped
        )

    filtered = "".join(kept)

    if len(filtered) > MAX_DIFF_LENGTH:
        filtered = filtered[:MAX_DIFF_LENGTH]
        filtered += "\n\n...[DIFF TRUNCATED TO PREVENT TOKEN OVERFLOW]..."
        logger.warning(
            "Token guardrail: diff exceeded %d chars and was truncated.",
            MAX_DIFF_LENGTH,
        )

    return filtered


# Entity types that will be detected and replaced.
# NOTE: "URL" is excluded from Presidio's NLP entities because .py is the ccTLD
# for Paraguay, causing standard file paths (e.g. server.py, app.py) to be
# misclassified as web URLs and replaced by <URL>, destroying file references in diffs.
_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "IP_ADDRESS",
    "CREDIT_CARD",
    "CRYPTO",
    "IBAN_CODE",
    "US_SSN",
]

# Simple regex patterns for common secret formats that Presidio's NLP models
# may not catch reliably (API keys, bearer tokens, private keys, etc.).
#
# NOTE: The generic 32-128 character alphanumeric pattern has been intentionally
# removed.  It is too broad and destructively replaces function names, variable
# names, hashes, and other syntactically meaningful code tokens, causing the
# downstream agents to receive mangled source code.  Presidio's NLP engine is
# the primary mechanism for entity detection.  The patterns retained here are
# narrow and anchored to well-known secret formats only.
_SECRET_PATTERNS: list[tuple[str, str]] = [
    # PEM private key blocks
    (r'-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----', "<REDACTED_PRIVATE_KEY>"),
    # AWS-style access key IDs (highly specific prefix + fixed 16-char suffix)
    (r'(?<![A-Z0-9])(AKIA|AGPA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])', "<REDACTED_AWS_KEY>"),
    # Generic "password = ..." or "secret = ..." assignments
    (r'(?i)(password|passwd|secret|token|api_key|apikey)\s*[:=]\s*["\']?[^\s"\']{6,}["\']?', "<REDACTED_SECRET>"),
]


@lru_cache(maxsize=1)
def _get_engines() -> tuple[AnalyzerEngine, AnonymizerEngine]:
    """Return the Presidio analyzer/anonymizer pair, initialised once per process."""
    logger.info("Privacy: initialising Presidio engines (one-time startup cost).")
    analyzer = AnalyzerEngine()
    anonymizer = AnonymizerEngine()
    logger.info("Privacy: Presidio engines ready.")
    return analyzer, anonymizer


def _apply_regex_scrubbing(text: str) -> str:
    """Apply the simple regex patterns for secrets not covered by Presidio NLP."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.DOTALL)
    return text


async def run_anonymizer(diff: str) -> str:
    """
    Detect and replace PII and secrets in *diff* before it reaches any LLM.

    Pipeline order
    --------------
    1. **Token guardrail** — strip bloated generated files and hard-cap the
       diff at ``MAX_DIFF_LENGTH`` characters to bound token spend.
    2. **Presidio NLP** — detect and replace standard PII entities.
    3. **Regex scrubbing** — redact API keys, passwords, and PEM blocks that
       Presidio's NLP models may miss.

    Args:
        diff: Raw Git diff string from the PR payload.

    Returns:
        Sanitized diff string with all detected entities replaced by type
        labels (e.g. ``<EMAIL_ADDRESS>``, ``<REDACTED_TOKEN>``).

    Raises:
        RuntimeError: If Presidio fails for any reason.  The pipeline is
            aborted to enforce a fail-closed security posture — the raw
            (potentially PII-containing) diff is never forwarded to an LLM.
    """
    if not diff:
        return diff

    # Step 1: Token guardrail — filter bloat and enforce the character cap.
    # This runs unconditionally, outside the try/except, so a Presidio failure
    # never bypasses the cost-protection logic.
    diff = _filter_and_truncate_diff(diff)

    try:
        analyzer, anonymizer = _get_engines()

        # Step 2: Presidio NLP-based PII detection and anonymisation.
        analysis_results = analyzer.analyze(
            text=diff,
            entities=_ENTITIES,
            language="en",
        )

        if analysis_results:
            anonymized = anonymizer.anonymize(
                text=diff,
                analyzer_results=analysis_results,  # type: ignore[arg-type]
            )
            sanitized = anonymized.text
            logger.info(
                "Privacy: anonymized %d entity/entities in diff.",
                len(analysis_results),
            )
        else:
            sanitized = diff
            logger.info("Privacy: no PII entities detected by Presidio.")

        # Step 3: Regex scrubbing for secrets (API keys, passwords, PEM blocks).
        sanitized = _apply_regex_scrubbing(sanitized)

        return sanitized

    except Exception as exc:
        logger.critical(
            "Privacy: Presidio anonymizer failed (%s) — aborting pipeline to "
            "prevent PII leakage. Raw diff will NOT be forwarded to any LLM.",
            exc,
        )
        raise RuntimeError(
            "Privacy Anonymizer failed. Pipeline aborted to prevent PII leakage."
        ) from exc
