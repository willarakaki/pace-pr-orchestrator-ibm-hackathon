"""
tests/test_config.py
──────────────────────────────────────────────────────────────────────────────
Unit tests for src/core/config.py.

Strategy
--------
The config module reads environment variables *at import time*, which means we
cannot simply import it at the top of this file — the module is already loaded
and its constants frozen.  Instead every test that exercises the guard logic
uses ``importlib.reload`` after patching ``os.environ``, so each scenario sees
a clean slate.
"""

import importlib
import os
import sys

import pytest


def _reload_config(env_overrides: dict) -> object:
    """
    Reload src.core.config with a controlled environment.

    The caller passes only the variables it cares about; everything else is
    temporarily removed so tests are hermetic.
    """
    # Remove the cached module so reload() re-executes module-level code.
    sys.modules.pop("src.core.config", None)

    # Patch only the env vars relevant to config.py.
    CONTROLLED_KEYS = {
        "WATSONX_URL",
        "WATSONX_APIKEY",
        "WATSONX_PROJECT_ID",
        "GITHUB_TOKEN",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
    }
    original = {k: os.environ.get(k) for k in CONTROLLED_KEYS}
    for key in CONTROLLED_KEYS:
        os.environ.pop(key, None)
    for key, value in env_overrides.items():
        os.environ[key] = value

    try:
        import src.core.config as cfg
        importlib.reload(cfg)
        return cfg
    finally:
        # Restore environment to avoid polluting other tests.
        for key in CONTROLLED_KEYS:
            os.environ.pop(key, None)
        for key, value in original.items():
            if value is not None:
                os.environ[key] = value


# ── Required variable guard ───────────────────────────────────────────────────

class TestRequiredVariable:
    """_require() must raise RuntimeError for missing or blank variables."""

    def test_missing_watsonx_url_raises(self):
        """Startup must fail fast when WATSONX_URL is absent."""
        with pytest.raises(RuntimeError, match="WATSONX_URL"):
            _reload_config({"WATSONX_APIKEY": "key", "WATSONX_PROJECT_ID": "id"})  # no WATSONX_URL set

    def test_empty_watsonx_url_raises(self):
        """A variable set to whitespace must be treated as absent."""
        with pytest.raises(RuntimeError, match="WATSONX_URL"):
            _reload_config({"WATSONX_URL": "   ", "WATSONX_APIKEY": "key", "WATSONX_PROJECT_ID": "id"})

    def test_valid_watsonx_url_accepted(self):
        """A non-empty WATSONX_URL must be loaded without error."""
        cfg = _reload_config({"WATSONX_URL": "https://us-south.ml.cloud.ibm.com", "WATSONX_APIKEY": "key", "WATSONX_PROJECT_ID": "id"})
        assert cfg.WATSONX_URL == "https://us-south.ml.cloud.ibm.com"

    def test_error_message_includes_variable_name(self):
        """RuntimeError message should name the missing variable for DX."""
        with pytest.raises(RuntimeError) as exc_info:
            _reload_config({})
        assert "WATSONX_URL" in str(exc_info.value)
        assert "not set" in str(exc_info.value)


# ── Optional variable defaults ────────────────────────────────────────────────

class TestOptionalVariables:
    """_optional() must fall back gracefully when variables are unset."""

    def test_github_token_defaults_to_empty_string(self):
        cfg = _reload_config({"WATSONX_URL": "url", "WATSONX_APIKEY": "key", "WATSONX_PROJECT_ID": "id"})
        assert cfg.GITHUB_TOKEN == ""

    def test_llm_timeout_defaults_to_100(self):
        cfg = _reload_config({"WATSONX_URL": "url", "WATSONX_APIKEY": "key", "WATSONX_PROJECT_ID": "id"})
        assert cfg.LLM_TIMEOUT_SECONDS == 100.0

    def test_llm_max_retries_defaults_to_2(self):
        cfg = _reload_config({"WATSONX_URL": "url", "WATSONX_APIKEY": "key", "WATSONX_PROJECT_ID": "id"})
        assert cfg.LLM_MAX_RETRIES == 2

    def test_optional_values_respect_env_override(self):
        cfg = _reload_config({
            "WATSONX_URL": "url",
            "WATSONX_APIKEY": "key",
            "WATSONX_PROJECT_ID": "id",
            "GITHUB_TOKEN": "ghp_abc123",
            "LLM_TIMEOUT_SECONDS": "42",
            "LLM_MAX_RETRIES": "7",
        })
        assert cfg.GITHUB_TOKEN == "ghp_abc123"
        assert cfg.LLM_TIMEOUT_SECONDS == 42.0
        assert cfg.LLM_MAX_RETRIES == 7


# ── Type correctness ──────────────────────────────────────────────────────────

class TestTypesAndWhitespaceTrimming:
    """Constants must be the right Python types and leading/trailing spaces stripped."""

    def test_timeout_is_float(self):
        cfg = _reload_config({"WATSONX_URL": "url", "WATSONX_APIKEY": "key", "WATSONX_PROJECT_ID": "id", "LLM_TIMEOUT_SECONDS": "55"})
        assert isinstance(cfg.LLM_TIMEOUT_SECONDS, float)

    def test_max_retries_is_int(self):
        cfg = _reload_config({"WATSONX_URL": "url", "WATSONX_APIKEY": "key", "WATSONX_PROJECT_ID": "id", "LLM_MAX_RETRIES": "2"})
        assert isinstance(cfg.LLM_MAX_RETRIES, int)

    def test_watsonx_url_is_stripped(self):
        cfg = _reload_config({"WATSONX_URL": "  https://trimmed.example.com  ", "WATSONX_APIKEY": "key", "WATSONX_PROJECT_ID": "id"})
        assert cfg.WATSONX_URL == "https://trimmed.example.com"
