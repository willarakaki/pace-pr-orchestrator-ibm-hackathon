# Project Coding Rules (Non-Obvious Only)

- **No test runner exists** — there are no unit tests, no pytest config, no CI. Manual validation only.
- All subagent task functions must be `async def` returning a `dict`; `safe_execute_task` in `orchestrator.py` passes `*args` directly, so signature must match `(diff: str)`.
- The `antigravity` package is a hackathon mock — do not attempt to install or import it unconditionally. The try/except fallback in `main.py` is intentional and must be preserved.
- Add new subagents by: (1) creating `src/subagents/<name>/` with `<name>_task.py` and `AGENTS.md`, (2) importing and adding to the `tasks` list in `orchestrator.py`. The pattern is strictly parallel — no sequential chaining.
- Subagent output dicts must exactly match the keys defined in their `AGENTS.md` — these are the LLM output contracts, not just documentation.
- All imports use `src.*` absolute package paths (not relative). The working directory when running must be `pace-pr-orchestrator/`.
- 110-second timeout per subagent is set in `safe_execute_task` — adjust there if needed, not inside individual task files.
