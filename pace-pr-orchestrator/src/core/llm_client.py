"""
Unified, fault-tolerant async LLM HTTP client (LLM Factory).

All subagents call ``llm_chat()`` instead of managing their own aiohttp
sessions and retry logic.  This centralises:

  - Authentication headers (sourced from src.core.config)
  - Retry-with-backoff for transient 5xx / network errors (via tenacity)
  - Pydantic output firewall: strips Markdown fences, JSON-parses, and
    validates the LLM string against the caller-supplied ``response_model``.
    A ``ValueError`` from validation triggers a tenacity retry so that
    hallucinated or prompt-injected payloads are automatically regenerated.
  - High-availability failover: if the primary provider exhausts all retries
    (or returns an unrecoverable error), the client switches to the
    fallback credentials and re-attempts the generation once more.
  - Integration with IBM WatsonX via langchain-ibm.
"""

import asyncio
import json
import logging
import re
from typing import Any, Type, TypeVar, cast, overload
import os

import aiohttp

from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from langchain_ibm import ChatWatsonx
from ibm_watsonx_ai.foundation_models.schema import TextChatParameters

from src.core.config import (
    WATSONX_APIKEY,
    WATSONX_PROJECT_ID,
    WATSONX_URL,
    LLM_FALLBACK_API_KEY,
    LLM_FALLBACK_API_URL,
    LLM_FALLBACK_PROJECT_ID,
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ── Pydantic output firewall ──────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _extract_json_str(text: str) -> str:
    """
    Strip Markdown code fences from *text* and return the inner JSON string.
    """
    if not isinstance(text, str):
        text = str(text)
    match = _FENCE_RE.search(text)
    return match.group(1) if match else text.strip()


def _parse_and_validate(raw_text: str, response_model: Type[T]) -> T:
    """
    Parse *raw_text* as JSON and validate it through *response_model*.
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


# ── Retry predicate ───────────────────────────────────────────────────────────

def _is_retryable(exc: BaseException) -> bool:
    """
    Return True for transient errors that warrant a retry.
    """
    if isinstance(exc, (asyncio.TimeoutError, ValueError)):
        return True
    
    # Simple check for HTTP 429 and 5xx errors from underlying layers
    exc_str = str(exc)
    if "429" in exc_str or "500" in exc_str or "502" in exc_str or "503" in exc_str or "504" in exc_str:
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


# ── IBM IAM token exchange ────────────────────────────────────────────────────

async def _get_ibm_iam_token(api_key: str) -> str:
    """
    Exchange an IBM Cloud API key for a short-lived IAM access token.

    POSTs to https://iam.cloud.ibm.com/identity/token and returns the
    ``access_token`` string from the JSON response.
    """
    url = "https://iam.cloud.ibm.com/identity/token"
    data = {
        "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
        "apikey": api_key,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data) as resp:
            resp.raise_for_status()
            payload = await resp.json()
    return payload["access_token"]


# ── Core single-provider request ─────────────────────────────────────────────

async def _do_request(
    messages: list[dict],
    api_url: str,
    api_key: str,
    project_id: str,
) -> str:
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 4000
    }
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    if "cloud.ibm.com" in api_url:
        iam_token = await _get_ibm_iam_token(api_key)
        headers["Authorization"] = f"Bearer {iam_token}"
        if project_id:
            payload["project_id"] = project_id
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            api_url, 
            json=payload, 
            headers=headers, 
            timeout=aiohttp.ClientTimeout(total=LLM_TIMEOUT_SECONDS)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


# ── Tenacity-wrapped primary call ─────────────────────────────────────────────

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
        raw_content = await _do_request(
            messages, WATSONX_URL, WATSONX_APIKEY, WATSONX_PROJECT_ID
        )
        if response_model is not None:
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
    Send a chat-completion request to WatsonX, validate the
    response through an optional Pydantic firewall, and return the result.
    """
    _primary_call = _make_primary_call(response_model)

    try:
        result = await _primary_call(messages)
    except Exception as primary_exc:
        if not LLM_FALLBACK_API_URL or not LLM_FALLBACK_API_KEY or not LLM_FALLBACK_PROJECT_ID:
            raise

        logger.warning(
            "Primary LLM provider failed after all retries (%s). "
            "Switching to fallback provider: %s",
            primary_exc,
            LLM_FALLBACK_API_URL,
        )
        raw_content = await _do_request(
            messages,
            LLM_FALLBACK_API_URL,
            LLM_FALLBACK_API_KEY,
            LLM_FALLBACK_PROJECT_ID
        )
        if response_model is not None:
            return cast(T, _parse_and_validate(raw_content, response_model))
        return raw_content
    else:
        if response_model is None:
            return cast(str, result)
        return cast(T, result)
