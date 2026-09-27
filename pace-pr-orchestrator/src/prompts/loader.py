"""
Prompt loader utility.

Reads prompt files from the src/prompts/ directory at runtime so the
subagents are never coupled to hardcoded string literals.
"""

import functools
from pathlib import Path

# Absolute path to the directory that holds the .md prompt files.
_PROMPTS_DIR = Path(__file__).parent


@functools.lru_cache(maxsize=None)
def load_prompt(filename: str) -> str:
    """
    Return the contents of *filename* from the prompts directory.

    Results are cached after the first read so there is no repeated I/O
    during a request burst.

    Args:
        filename: Bare filename including extension, e.g. ``"security_prompt.md"``.

    Returns:
        The prompt text as a stripped string.

    Raises:
        FileNotFoundError: if the file does not exist in src/prompts/.
    """
    path = _PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8").strip()
