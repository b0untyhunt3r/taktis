"""Tests for taktis_engine.config — Settings loading, YAML, env vars, coercion."""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch

from taktis.config import _coerce, _load_yaml_config, Settings


# ---------------------------------------------------------------------------
# _coerce
# ---------------------------------------------------------------------------

class TestCoerce:
    """Tests for _coerce with both string type hints (PEP 563) and type objects."""

    def test_int_from_string_hint(self):
        assert _coerce("42", "int") == 42

    def test_int_from_type_object(self):
        assert _coerce("42", int) == 42

    def test_int_from_int(self):
        assert _coerce(10, "int") == 10

    def test_float_from_string(self):
        assert _coerce("3.14", "float") == pytest.approx(3.14)

    def test_float_from_type_object(self):
        assert _coerce("3.14", float) == pytest.approx(3.14)

    def test_bool_true_variants(self):
        for v in ("true", "True", "1", "yes", "YES"):
            assert _coerce(v, "bool") is True

    def test_bool_false_variants(self):
        for v in ("false", "0", "no", ""):
            assert _coerce(v, "bool") is False

    def test_bool_from_bool(self):
        assert _coerce(True, "bool") is True
        assert _coerce(False, "bool") is False

    def test_string_passthrough(self):
        assert _coerce("hello", "str") == "hello"

    def test_unknown_type_passthrough(self):
        assert _coerce("anything", "SomeCustomType") == "anything"

    def test_int_invalid_raises(self):
        with pytest.raises(ValueError):
            _coerce("not_a_number", "int")

    def test_float_invalid_raises(self):
        with pytest.raises(ValueError):
            _coerce("not_a_float", "float")


# ---------------------------------------------------------------------------
# _load_yaml_config
# ---------------------------------------------------------------------------

class TestLoadYamlConfig:
    def test_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _load_yaml_config()
        assert result == {}

    def test_loads_valid_yaml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text(
            "max_concurrent_tasks: 42\nlog_level: DEBUG\n",
            encoding="utf-8",
        )
        result = _load_yaml_config()
        assert result["max_concurrent_tasks"] == 42
        assert result["log_level"] == "DEBUG"

    def test_returns_empty_for_non_dict_yaml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
        result = _load_yaml_config()
        assert result == {}

    def test_returns_empty_for_empty_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("", encoding="utf-8")
        result = _load_yaml_config()
        assert result == {}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class TestSettings:
    def test_default_values(self):
        """Settings should have sane defaults even without config file or env."""
        with patch.dict(os.environ, {}, clear=False):
            # Remove any TAKTIS_ env vars that may leak from the test environment
            env = {k: v for k, v in os.environ.items() if not k.startswith("TAKTIS_")}
            env["TAKTIS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
            with patch.dict(os.environ, env, clear=True):
                with patch("taktis.config._load_yaml_config", return_value={}):
                    s = Settings()
                    assert s.max_concurrent_tasks == 15
                    assert s.default_model == "sonnet"
                    assert s.default_permission_mode == "auto"
                    assert s.log_level == "INFO"
                    assert s.phase_timeout == 14400
                    assert s.admin_api_key == ""

    def test_env_var_override(self):
        env = {
            "TAKTIS_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "TAKTIS_MAX_CONCURRENT_TASKS": "99",
            "TAKTIS_LOG_LEVEL": "DEBUG",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("taktis.config._load_yaml_config", return_value={}):
                s = Settings()
                assert s.max_concurrent_tasks == 99
                assert s.log_level == "DEBUG"

    def test_yaml_override(self):
        yaml_cfg = {"max_concurrent_tasks": 7, "default_model": "opus"}
        env = {"TAKTIS_DATABASE_URL": "sqlite+aiosqlite:///:memory:"}
        with patch.dict(os.environ, env, clear=False):
            # Remove TAKTIS_ vars that would override yaml
            clean_env = {k: v for k, v in os.environ.items()
                         if not k.startswith("TAKTIS_") or k == "TAKTIS_DATABASE_URL"}
            with patch.dict(os.environ, clean_env, clear=True):
                with patch("taktis.config._load_yaml_config", return_value=yaml_cfg):
                    s = Settings()
                    assert s.max_concurrent_tasks == 7
                    assert s.default_model == "opus"

    def test_env_overrides_yaml(self):
        """Env vars take priority over YAML."""
        yaml_cfg = {"max_concurrent_tasks": 7}
        env = {
            "TAKTIS_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "TAKTIS_MAX_CONCURRENT_TASKS": "25",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("taktis.config._load_yaml_config", return_value=yaml_cfg):
                s = Settings()
                assert s.max_concurrent_tasks == 25
