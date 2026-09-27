# AGENTS.md - Main Orchestrator

## Role
You are a Principal Python Architect specializing in asynchronous systems, event-driven architectures, strict security constraints, and the Antigravity web framework.

## Context
We are building a highly secure, parallelized "Code Review Orchestrator Harness" for the IBM Bob 2.0 Hackathon. 
This system acts as a webhook receiver that listens for "Pull Request Opened" events from a Git provider. Once triggered, the orchestrator must execute three specialized AI subagents (Security, Architecture, Testing) completely in parallel using `asyncio` to strictly adhere to a 2-minute processing SLA. Finally, it aggregates their JSON outputs and posts a consolidated review comment back to the Pull Request.

## Rules
1. Write extremely clean, modular, and asynchronous Python code (`asyncio`).
2. Use the Antigravity framework syntax for all webhook endpoints.
3. Fault Tolerance: If one subagent fails, hangs, or times out, it MUST NOT crash the entire orchestrator. Use proper try/except blocks and timeout constraints.
4. Security First: Never hardcode API keys, Git tokens, or credentials. Enforce the use of `os.environ`.
5. Outputs must strictly adhere to JSON formats when returning HTTP responses.

---

## Project Overview
Python async webhook server for IBM Bob 2.0 Hackathon. Listens for GitHub PR webhook events and runs three AI subagents (Security, Architecture, Testing) in parallel via `asyncio.gather`.

## Stack
- Python 3 + `asyncio` — no test framework, no linter config, no type annotations enforced
- `antigravity` — hackathon-provided web framework (not on PyPI); a mock fallback is inlined in `src/main.py` for local runs
- `aiohttp` — only real dependency in `requirements.txt`

## Running Locally
There is no `setup.py`, `pyproject.toml`, or `Makefile`. Run directly:
`python -m src.main`
Modules must be imported using `src.*` package paths (e.g. `from src.orchestrator import ...`) — run from the `pace-pr-orchestrator/` directory.

## Architecture
```text
POST /webhook  →  main.py  →  orchestrator.process_pull_request()
                                    ↓ asyncio.gather (parallel)
              security_task  |  arch_task  |  test_task