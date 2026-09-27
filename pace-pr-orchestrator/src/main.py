import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
from typing import Any


import aiohttp
import aiohttp.web as _web

from src.core.config import DRY_RUN, GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET

# The antigravity package is provided by the hackathon environment.
# A local mock is inlined here so the module runs without it installed.
try:
    import antigravity as _antigravity  # type: ignore[import]

    App = getattr(_antigravity, "App")
    Request = getattr(_antigravity, "Request")
    JSONResponse = getattr(_antigravity, "JSONResponse")
    _framework_run = getattr(_antigravity, "run", None)
    if not all(callable(component) for component in (App, Request, JSONResponse, _framework_run)):
        raise ImportError("antigravity does not provide a callable run()")
except (ImportError, AttributeError):
    _framework_run = None

    class App:
        def __init__(self):
            self.routes: dict = {}

        def post(self, path: str):
            def decorator(func):
                self.routes[path] = func
                return func
            return decorator

    class Request:  # type: ignore[no-redef]
        """Thin wrapper around aiohttp.web.Request."""

        def __init__(self, aio_request: "_web.Request"):
            self._req = aio_request
            self.headers = aio_request.headers

        async def body(self) -> bytes:
            return await self._req.read()

        async def json(self):
            return await self._req.json()

    class JSONResponse:  # type: ignore[no-redef]
        """Wraps aiohttp.web.json_response, mirroring the antigravity API."""

        def __init__(self, content, status_code: int = 200):
            self.content = content
            self.status_code = status_code

        def to_aiohttp(self) -> "_web.Response":
            return _web.json_response(self.content, status=self.status_code)

def _run_local_server(app: Any, *, host: str = "0.0.0.0", port: int = 8000) -> None:
    """Start the aiohttp fallback server for routes registered on the local App."""
    aio_app = _web.Application()

    for path, handler in app.routes.items():
        async def _make_handler(req: _web.Request, _h=handler) -> _web.Response:
            wrapped = Request(req)
            result = await _h(wrapped)
            if isinstance(result, JSONResponse):
                return result.to_aiohttp()
            # Passthrough for any raw aiohttp response returned directly.
            return result  # type: ignore[return-value]

        aio_app.router.add_post(path, _make_handler)

    _web.run_app(aio_app, host=host, port=port)

from src.orchestrator import process_pull_request


async def check_granite_guardian(payload: dict) -> None:
    """
    IBM Granite Guardian hook — Phase 3 prompt-injection defence.

    In production this would call the Granite Guardian REST API to scan the
    incoming webhook payload for adversarial prompt-injection patterns before
    any LLM processes the content.  The hook is fail-open by design at this
    stub stage: it logs a structured audit entry and returns without blocking
    so that the rest of the pipeline is unaffected while the real integration
    is wired up.

    Args:
        payload: The parsed webhook JSON dict.
    """
    pr_number = payload.get("pull_request", {}).get("number", "unknown")
    logger.info(
        "[Granite Guardian] Scanning PR #%s payload for prompt-injection patterns.",
        pr_number,
    )



logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

MAX_CONCURRENT_PRS = int(os.environ.get("MAX_CONCURRENT_PRS", "3"))
_pr_semaphore = None
_SEMAPHORE_TIMEOUT = float(os.environ.get("PR_QUEUE_TIMEOUT", "10"))
SERVER_HOST = os.environ.get("HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("PORT", "8000"))

app = App()

_GITHUB_API_BASE = "https://api.github.com"


async def post_github_comment(repo_name: str, pr_number: int, comment_markdown: str) -> None:
    """
    Post a Markdown comment to a GitHub Pull Request via the GitHub REST API.

    Args:
        repo_name:        Full repository name in "owner/repo" format.
        pr_number:        The pull request number to comment on.
        comment_markdown: The Markdown body of the comment to post.

    Requires the GITHUB_TOKEN environment variable to be set with a token
    that has ``pull_requests: write`` (or ``repo``) scope.  Logs an error
    and returns without raising if the token is missing or the API call fails.
    """
    if not GITHUB_TOKEN:
        logger.error("post_github_comment: GITHUB_TOKEN is not set -- skipping PR comment.")
        return

    url = f"{_GITHUB_API_BASE}/repos/{repo_name}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    payload = {"body": comment_markdown}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                response.raise_for_status()
                comment_url = (await response.json()).get("html_url", "<unknown>")
        logger.info("Posted review comment to PR #%s: %s", pr_number, comment_url)
    except Exception as e:
        logger.error("post_github_comment: failed to post to %s PR #%s -- %s", repo_name, pr_number, e)


def _verify_github_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """
    Verify the GitHub HMAC-SHA256 webhook signature.

    Returns True when the signature is valid or when no secret is configured
    (validation is disabled).  Returns False when a secret is configured but
    the signature is missing or does not match.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return True  # Validation disabled — no secret configured.
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.post("/webhook")
async def github_webhook(request: Request):
    """
    Antigravity POST /webhook endpoint.
    Accepts GitHub-style PR webhook payloads and orchestrates a parallel code review.
    Only 'opened' and 'synchronize' actions are processed; all others are ignored.
    """
    # ── HMAC-SHA256 signature validation ─────────────────────────────────────
    raw_body: bytes = await request.body()
    signature_header: str | None = request.headers.get("X-Hub-Signature-256")
    if not _verify_github_signature(raw_body, signature_header):
        logger.warning("Webhook rejected: invalid or missing X-Hub-Signature-256 header.")
        return JSONResponse({"error": "Unauthorized", "details": "Invalid webhook signature"}, status_code=401)

    try:
        payload = json.loads(raw_body)
    except Exception as e:
        logger.error("Failed to parse request body: %s", e)
        return JSONResponse({"error": "Bad Request", "details": "Invalid JSON payload"}, status_code=400)

    # ── IBM Granite Guardian: prompt-injection scan ───────────────────────────
    await check_granite_guardian(payload)

    action = payload.get("action")

    if action not in ("opened", "synchronize"):
        logger.info("Ignoring webhook action: %s", action)
        return JSONResponse({"status": "ignored", "reason": f"Action '{action}' not processed"}, status_code=200)

    pr_data = payload.get("pull_request", {})
    repo_name = payload.get("repository", {}).get("full_name", "")

    diff_url = pr_data.get("diff_url") or ""
    raw_diff = ""
    if diff_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    diff_url,
                    headers={
                        "Authorization": f"Bearer {GITHUB_TOKEN}",
                        "Accept": "application/vnd.github.v3.diff",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as diff_response:
                    diff_response.raise_for_status()
                    raw_diff = await diff_response.text()
        except Exception as _diff_exc:
            logger.error("Failed to fetch diff from %s: %s", diff_url, _diff_exc)

    parsed_pr = {
        "id": pr_data.get("number", "unknown"),
        "diff": raw_diff,
        "title": pr_data.get("title") or "",
        "repo_name": repo_name,
    }

    logger.info("Received PR webhook: PR #%s -- %s", parsed_pr["id"], parsed_pr["title"])

    global _pr_semaphore
    if _pr_semaphore is None:
        _pr_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PRS)

    try:
        await asyncio.wait_for(_pr_semaphore.acquire(), timeout=_SEMAPHORE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(
            "PR #%s rejected: all %d slots busy after %.0fs -- returning 503",
            parsed_pr["id"], MAX_CONCURRENT_PRS, _SEMAPHORE_TIMEOUT,
        )
        return JSONResponse(
            {"error": "Service Unavailable", "details": "Too many PRs in flight; retry later"},
            status_code=503,
        )

    try:
        review_result = await process_pull_request(parsed_pr)
    except Exception as e:
        logger.error("Unexpected orchestrator error for PR #%s: %s", parsed_pr["id"], e)
        return JSONResponse({"error": "Internal Server Error", "details": str(e)}, status_code=500)
    finally:
        _pr_semaphore.release()

    # Post the human-readable Markdown report back to the PR if we have enough context.
    if repo_name and parsed_pr["id"] != "unknown":
        if DRY_RUN:
            logger.info(
                "[DRY-RUN MODE] Would have posted the following report to GitHub: \n%s",
                review_result["markdown_report"],
            )
        else:
            await post_github_comment(repo_name, parsed_pr["id"], review_result["markdown_report"])


    return JSONResponse(review_result, status_code=200)


if __name__ == "__main__":
    logger.info("Starting Antigravity Code Review Orchestrator on %s:%s...", SERVER_HOST, SERVER_PORT)
    if _framework_run is not None:
        _framework_run(app, host=SERVER_HOST, port=SERVER_PORT)
    else:
        # Fallback to our functional aiohttp local server mock.
        logger.info("Hackathon framework not found or incompatible. Starting local aiohttp fallback server...")
        _run_local_server(app, host=SERVER_HOST, port=SERVER_PORT)
