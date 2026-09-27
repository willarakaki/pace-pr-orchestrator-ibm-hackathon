# Project Architecture Rules (Non-Obvious Only)

- All three subagents are **always run in parallel** regardless of PR content — there is no conditional dispatch or dependency between them.
- Subagent failures are swallowed by `safe_execute_task`; the aggregated report always returns all three keys (`security`, `architecture`, `testing`) even when a subagent errors.
- There is no persistence layer — results are returned in the HTTP response only; nothing is stored.
- The 400-line threshold for "High Risk" PRs is duplicated in both `arch_task.py` logic and `AGENTS.md` — both must stay in sync if changed.
- Extending to a real LLM: each `*_task.py` is the only file to modify; the orchestration layer does not need to change. `BOB_API_KEY` env var is the expected auth mechanism.
- No database, no queue, no retry logic outside the 110s timeout — the design is intentionally stateless and single-request.
