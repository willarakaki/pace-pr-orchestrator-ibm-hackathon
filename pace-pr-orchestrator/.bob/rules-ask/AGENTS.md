# Project Documentation Rules (Non-Obvious Only)

- `src/subagents/*/AGENTS.md` files are **LLM prompt contracts**, not developer docs — they define the exact JSON schema each subagent must return.
- `antigravity` is a fictional/hackathon web framework not on PyPI; treat it like FastAPI for routing concepts but expect it to be absent locally.
- The only real HTTP dependency is `aiohttp` (in `requirements.txt`) — `antigravity` is listed there but is a placeholder.
- PR "diff" is mocked from `pull_request.body` in the webhook payload, not from a real diff API call.
- `bob_sessions/` exists solely to store Bob session screenshots — it has no runtime significance.
