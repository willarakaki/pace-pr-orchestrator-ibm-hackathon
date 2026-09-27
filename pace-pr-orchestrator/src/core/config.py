"""
Centralised configuration loader.

All environment variables are read exactly once at import time and exposed as
module-level constants.  Any subagent or utility that needs a credential
imports from here — never calls os.environ directly.

Raises RuntimeError at startup if a required variable is absent so the
container fails fast with a clear message rather than silently misbehaving at
request time.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ── Required variables ────────────────────────────────────────────────────────

def _require(name: str) -> str:
    """Return the value of *name* or raise RuntimeError if it is unset/empty."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is not set. "
            "Set it in your .env file or container environment before starting."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    """Return the value of *name*, or *default* if absent."""
    return os.environ.get(name, default).strip()


# ── Public constants ───────────────────────────────────────────────────────────

# URL of the Watsonx endpoint.
WATSONX_URL: str = _require("WATSONX_URL")

# API key for Watsonx.
WATSONX_APIKEY: str = _require("WATSONX_APIKEY")

# Project ID for Watsonx (required for IBM Watsonx endpoints, leave empty for
# standard OpenAI-compatible endpoints that do not require it).
WATSONX_PROJECT_ID: str = _optional("WATSONX_PROJECT_ID", "")

# Model identifier forwarded in the chat-completion payload.
LLM_MODEL: str = _optional("LLM_MODEL", "ibm/granite-3-8b-instruct")

# ── Fallback (secondary) LLM provider ────────────────────────────────────────
# When set, llm_client will automatically failover to this endpoint after the
# primary provider exhausts its retry budget or returns an unrecoverable error
# (503 / 429).  Leave unset to disable the failover path entirely.

# URL of the fallback LLM inference endpoint.
LLM_FALLBACK_API_URL: str = _optional("LLM_FALLBACK_API_URL")

# Bearer token for the fallback LLM API (optional).
LLM_FALLBACK_API_KEY: str = _optional("LLM_FALLBACK_API_KEY")

# Project ID for the fallback LLM API (optional).
LLM_FALLBACK_PROJECT_ID: str = _optional("LLM_FALLBACK_PROJECT_ID")

# Personal access token used to post PR review comments on GitHub.
GITHUB_TOKEN: str = _optional("GITHUB_TOKEN")

# Secret used to validate GitHub webhook HMAC-SHA256 signatures.
# When set, every inbound webhook request must carry a valid X-Hub-Signature-256
# header; requests without a valid signature are rejected with 401 Unauthorized.
# Leave empty to disable signature validation (not recommended for production).
GITHUB_WEBHOOK_SECRET: str = _optional("GITHUB_WEBHOOK_SECRET", "")

# Per-request timeout for LLM calls (seconds).  Defaults to 100 s.
LLM_TIMEOUT_SECONDS: float = float(_optional("LLM_TIMEOUT_SECONDS", "100"))

# Number of retry attempts for transient LLM API failures.  Defaults to 2.
# Kept deliberately low: each retry re-sends the full (possibly large) diff to
# the LLM, so a high value causes exponential cost spirals when Pydantic
# validation fails repeatedly.  Override via LLM_MAX_RETRIES env var if needed.
LLM_MAX_RETRIES: int = int(_optional("LLM_MAX_RETRIES", "2"))

# ── Dry-run mode ──────────────────────────────────────────────────────────────

# When True, the pipeline runs end-to-end but the final GitHub comment is NOT
# posted.  The report is emitted to the application log instead, making this
# flag safe to use in staging, CI, and local development without polluting
# real PRs.  Set DRY_RUN=true (case-insensitive) in the environment to enable.
DRY_RUN: bool = _optional("DRY_RUN", "false").lower() in ("1", "true", "yes")
