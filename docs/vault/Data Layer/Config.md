---
title: Config
tags: [data, config]
---

# Config

File: `taktis/config.py`

`Settings` dataclass (`config.py:24–79`). Load priority (`44–57`):

1. **Environment variables** (`TAKTIS_*`) — highest
2. **`config.yaml`** — if present in working dir (`13–21`)
3. **Hardcoded defaults** (`34–42`)

Env-var strings are coerced to int/float/bool (`60–75`); bool matches `true`/`1`/`yes` case-insensitively.

## Fields

| Field | Default | Env var | Notes |
|---|---|---|---|
| `database_url` | `sqlite+aiosqlite:///taktis.db` | `TAKTIS_DATABASE_URL` | aiosqlite URL |
| `max_concurrent_tasks` | `15` | `TAKTIS_MAX_CONCURRENT_TASKS` | Semaphore in [[ProcessManager]] |
| `default_model` | `sonnet` | `TAKTIS_DEFAULT_MODEL` | Short name |
| `default_permission_mode` | `auto` | `TAKTIS_DEFAULT_PERMISSION_MODE` | SDK permission mode |
| `log_level` | `INFO` | `TAKTIS_LOG_LEVEL` | |
| `claude_command` | `claude` | `TAKTIS_CLAUDE_COMMAND` | |
| `phase_timeout` | `14400` (4h) | `TAKTIS_PHASE_TIMEOUT` | Max wait per wave |
| `db_pool_size` | `10` | `TAKTIS_DB_POOL_SIZE` | Recommend ≥ `max_concurrent_tasks + 2` |
| `admin_api_key` | `""` | `TAKTIS_ADMIN_API_KEY` | Gates `/admin` when set |

## Environment gotchas

- `CLAUDE_CODE_STREAM_CLOSE_TIMEOUT` is set to 4 hours inside `run.py` (documented in `CLAUDE.md`). This is an SDK-level env var, not a Taktis setting.
- `taktis.db` is WAL-mode SQLite. The sidecar `taktis.db-wal` and `taktis.db-shm` files are normal and are `.gitignore`d.

## Related

[[Database Schema]] · [[Repository]] · [[ProcessManager]]
