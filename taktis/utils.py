"""Shared utility functions used across Taktis modules."""

from __future__ import annotations

import json
from typing import Any


def parse_json_field(value: str | list | dict | None, default: Any = None) -> Any:
    """Parse a JSON string field, returning *default* if None or empty.

    Handles the common case of DB columns that store JSON as TEXT —
    the value may already be deserialized (list/dict) if coming from
    an enrichment layer, or still a raw JSON string from the DB.
    """
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default
