"""
Unified, fault-tolerant async LLM HTTP client (LLM Factory).

All subagents call ``llm_chat()`` instead of managing their own aiohttp
sessions and retry logic.  This centralises:

  - Authentication headers (sourced from src.core.config)
  - Retry-with-backoff for transient 5xx / network errors (via tenacity)
  - OpenAI-style chat-completion envelope unwrapping
  - Pydantic output firewall: strips Markdown fences, JSON-parses, and
    validates the LLM string against the caller-supplied ``response_model``.
    A ``ValueError`` from validation triggers a tenacity retry so that
    hallucinated or prompt-injected payloads are automatically regenerated.
  - High-availability failover: if the primary provider exhausts all retries
    (or returns an unrecoverable 503/429), the client switches to the
    fallback credentials and re-attempts the generation once more.
  - A single shared aiohttp.ClientSession (created lazily, reused per process)
"""

import asyncio
import json
import logging
import re
from typing import Any, Type, TypeVar, cast, overload

import aiohttp
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.core.config import (
    LLM_API_KEY,
    LLM_API_URL,
    LLM_FALLBACK_API_KEY,
    LLM_FALLBACK_API_URL,
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ── Shared session (lazy singleton) ──────────────────────────────────────────
_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    """Return the process-wide aiohttp session, creating it on first call."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


# ── Header / credential helpers ───────────────────────────────────────────────

def _build_headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


# ── Pydantic output firewall ──────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _extract_json_str(text: str) -> str:
    """
    Strip Markdown code fences from *text* and return the inner JSON string.

    Handles the common LLM habit of wrapping JSON in triple-backtick fences,
    e.g.::

        ```json
        {"status": "pass", ...}
        ```

    If no fence is found the original text is returned unchanged so that
    clean JSON responses still parse correctly.
    """
    match = _FENCE_RE.search(text)
    return match.group(1) if match else text.strip()


def _parse_and_validate(raw_text: str, response_model: Type[T]) -> T:
    """
    Parse *raw_text* as JSON and validate it through *response_model*.

    Raises:
        ValueError: When JSON cannot be decoded or Pydantic validation fails.
            Both error paths raise ``ValueError`` so that tenacity's
            ``retry_if_exception(ValueError)`` treats them as retryable faults,
            triggering a fresh LLM generation attempt.
    """
    cleaned = _extract_json_str(raw_text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned non-JSON content: {exc}\n---\n{cleaned[:200]}") from exc

    try:
        return response_model.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"LLM output failed Pydantic validation ({response_model.__name__}): {exc}"
        ) from exc


# ── HTTP response unwrapping ──────────────────────────────────────────────────

def _unwrap_content(raw: dict) -> str:
    """
    Extract the assistant message content string from an OpenAI-compatible
    chat-completion envelope, or serialise raw dicts for direct-JSON endpoints.
    """
    if "choices" in raw:
        return raw["choices"][0]["message"]["content"]
    # Direct-JSON endpoint: serialise back to string so the firewall can parse it.
    return json.dumps(raw)


# ── Retry predicate ───────────────────────────────────────────────────────────

def _is_retryable(exc: BaseException) -> bool:
    """
    Return True for transient errors that warrant a retry.

    Retryable:
    - ``asyncio.TimeoutError`` — request timed out.
    - ``aiohttp.ClientResponseError`` with status >= 500 — server-side fault.
    - Any other ``aiohttp.ClientError`` — connection/DNS/protocol error.
    - ``ValueError`` — Pydantic firewall rejected the LLM output (hallucination
      or prompt-injection attempt); a fresh generation may succeed.

    Not retryable:
    - ``aiohttp.ClientResponseError`` with status in 4xx (except 429) — caller
      bug or invalid input; retrying would not help.
    """
    if isinstance(exc, (asyncio.TimeoutError, ValueError)):
        return True
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status == 429 or exc.status >= 500
    if isinstance(exc, aiohttp.ClientError):
        return True
    return False


def _log_retry(retry_state) -> None:
    logger.warning(
        "LLM API call failed (%s) — attempt %d/%d, retrying in %.1fs.",
        retry_state.outcome.exception(),
        retry_state.attempt_number,
        LLM_MAX_RETRIES,
        retry_state.next_action.sleep,
    )


# ── Core single-provider request ─────────────────────────────────────────────

async def _do_request(
    messages: list[dict],
    api_url: str,
    api_key: str,
) -> str:
    """
    POST *messages* to *api_url* with *api_key* and return the raw content string.

    Raises aiohttp errors directly — callers are responsible for retry logic.
    """
    payload: dict[str, Any] = {"model": LLM_MODEL, "messages": messages}
    timeout = aiohttp.ClientTimeout(total=LLM_TIMEOUT_SECONDS)
    headers = _build_headers(api_key)

    session = _get_session()
    async with session.post(
        api_url,
        json=payload,
        headers=headers,
        timeout=timeout,
    ) as response:
        response.raise_for_status()
        raw = await response.json()

    return _unwrap_content(raw)


# ── Tenacity-wrapped primary call ─────────────────────────────────────────────

# ── Tenacity-wrapped primary call ─────────────────────────────────────────────
#
# The Pydantic validation step is intentionally performed INSIDE _primary_call
# so that a ValueError raised by _parse_and_validate is visible to Tenacity's
# retry predicate and triggers a fresh LLM generation attempt.  If validation
# were done in the outer llm_chat wrapper it would escape the @retry boundary
# and never be retried.
#
# _primary_call therefore needs to know which response_model to validate
# against.  We use a closure factory (_make_primary_call) rather than a
# module-level decorated function so that each call site binds its own model.

def _make_primary_call(response_model: "Type[T] | None"):
    """
    Return a Tenacity-retried coroutine that calls the primary provider and,
    when *response_model* is provided, validates the response inside the retry
    boundary so that Pydantic ``ValueError``s trigger a retry.
    """

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=60),
        stop=stop_after_attempt(LLM_MAX_RETRIES),
        before_sleep=_log_retry,
        reraise=True,
    )
    async def _primary_call(messages: list[dict]):
        raw_content = await _do_request(messages, LLM_API_URL, LLM_API_KEY)
        if response_model is not None:
            # Raises ValueError on bad JSON or schema mismatch — caught by @retry.
            return _parse_and_validate(raw_content, response_model)
        return raw_content

    return _primary_call


# ── Public interface ──────────────────────────────────────────────────────────

@overload
async def llm_chat(messages: list[dict], response_model: Type[T]) -> T: ...
@overload
async def llm_chat(messages: list[dict], response_model: None = ...) -> str: ...


async def llm_chat(
    messages: list[dict],
    response_model: Type[T] | None = None,
) -> T | str:
    """
    Send a chat-completion request to the configured LLM endpoint, validate the
    response through an optional Pydantic firewall, and return the result.

    Two-tier high-availability pattern
    -----------------------------------
    1. The primary provider is called up to ``LLM_MAX_RETRIES`` times (with
       exponential back-off) for any transient or firewall failure.  Pydantic
       validation is performed **inside** the retried function so that a
       ``ValueError`` from a malformed/hallucinated LLM response is caught by
       Tenacity and triggers a fresh generation attempt.
    2. If the primary exhausts its budget *and* ``LLM_FALLBACK_API_URL`` is
       configured, one further attempt is made against the fallback provider.
       The fallback attempt also goes through the Pydantic firewall.
    3. If the fallback itself fails, the exception propagates to the caller.

    Args:
        messages:       List of ``{"role": …, "content": …}`` dicts.
        response_model: Optional Pydantic ``BaseModel`` subclass.  When
                        supplied, the raw LLM string is stripped of Markdown
                        fences, JSON-parsed, and validated.  A ``ValueError``
                        from validation causes tenacity to retry the generation.
                        When ``None``, raw ``dict`` is returned (legacy mode).

    Returns:
        An instance of *response_model* when provided, otherwise a plain dict.

    Raises:
        Exception: After all primary retries are exhausted and either no
                   fallback is configured or the fallback also fails.
    """
    _primary_call = _make_primary_call(response_model)

    try:
        result = await _primary_call(messages)
    except Exception as primary_exc:
        if not LLM_FALLBACK_API_URL:
            raise

        logger.warning(
            "Primary LLM provider failed after all retries (%s). "
            "Switching to fallback provider: %s",
            primary_exc,
            LLM_FALLBACK_API_URL,
        )
        # One attempt on the fallback — errors propagate to the caller.
        raw_content = await _do_request(messages, LLM_FALLBACK_API_URL, LLM_FALLBACK_API_KEY)
        if response_model is not None:
            return cast(T, _parse_and_validate(raw_content, response_model))
        return raw_content
    else:
        if response_model is None:
            # result is a raw string; return it directly.
            return cast(str, result)
        return cast(T, result)
