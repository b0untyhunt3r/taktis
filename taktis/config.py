"""Application settings loaded from config.yaml and environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


def _load_yaml_config() -> dict[str, Any]:
    """Load settings from config.yaml if it exists."""
    config_path = Path("config.yaml")
    if config_path.is_file():
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
            if isinstance(data, dict):
                return data
    return {}


@dataclass
class Settings:
    """Application settings.

    Priority (highest to lowest):
      1. Environment variables (prefixed with ``TAKTIS_``)
      2. Values in ``config.yaml``
      3. Defaults defined here
    """

    database_url: str = "sqlite+aiosqlite:///taktis.db"
    max_concurrent_tasks: int = 15
    default_model: str = "sonnet"
    default_permission_mode: str = "auto"
    log_level: str = "INFO"
    claude_command: str = "claude"
    phase_timeout: int = 14400  # seconds (4 hours) — max wait time per wave in scheduler
    db_pool_size: int = 10  # DB connection pool size (>= max_concurrent_tasks + 2)
    admin_api_key: str = ""  # If set, /admin requires Authorization: Bearer <key>

    def __post_init__(self) -> None:
        # Load YAML config, then override with env vars
        yaml_cfg = _load_yaml_config()

        for f in fields(self):
            # YAML layer
            if f.name in yaml_cfg:
                setattr(self, f.name, f.type and _coerce(yaml_cfg[f.name], f.type))

            # Env var layer (TAKTIS_ prefix)
            env_key = f"TAKTIS_{f.name.upper()}"
            env_val = os.environ.get(env_key)
            if env_val is not None:
                setattr(self, f.name, _coerce(env_val, f.type))


def _coerce(value: Any, type_hint: Any) -> Any:
    """Coerce a value to the expected type.

    With ``from __future__ import annotations``, ``f.type`` is a string
    (e.g. ``'int'``), not the actual type object.  We compare against both
    forms for safety.
    """
    if type_hint is int or type_hint == "int":
        return int(value)
    if type_hint is float or type_hint == "float":
        return float(value)
    if type_hint is bool or type_hint == "bool":
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    return value


# Module-level singleton; import this wherever config is needed.
settings = Settings()
