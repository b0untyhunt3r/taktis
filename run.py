#!/usr/bin/env python3
"""Run the Taktis Web UI."""

import logging
import sys
import os
import time
import threading
from pathlib import Path

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Force UTF-8 everywhere — prevents cp1252 mojibake on Windows
os.environ.setdefault("PYTHONUTF8", "1")

# Prevent SDK stream timeout for long-running tasks (4 hours)
os.environ.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "14400000")


class _JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for machine-parseable output."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
        # Propagate task_id / project_id if present in the message
        for field in ("task_id", "project_id", "phase_id"):
            val = getattr(record, field, None)
            if val:
                entry[field] = val
        return json.dumps(entry, default=str)


def _setup_logging(json_log: bool = False) -> None:
    """Configure root logging — JSON or human-readable."""
    import logging
    if json_log:
        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter())
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    else:
        log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        log_datefmt = "%Y-%m-%d %H:%M:%S"
        logging.basicConfig(level=logging.INFO, format=log_fmt, datefmt=log_datefmt)


def run_web(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start the web UI in a daemon thread with Ctrl+C handling."""
    import logging
    import uvicorn
    from taktis.web.app import create_app

    json_log = "--json-log" in sys.argv
    _setup_logging(json_log)

    log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_datefmt = "%Y-%m-%d %H:%M:%S"

    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": log_fmt, "datefmt": log_datefmt},
            "access": {"format": log_fmt, "datefmt": log_datefmt},
        },
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": "default", "stream": "ext://sys.stderr"},
            "access": {"class": "logging.StreamHandler", "formatter": "access", "stream": "ext://sys.stdout"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }

    app = create_app()

    def _serve():
        uvicorn.run(app, host=host, port=port, log_level="info", log_config=log_config)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nShutting down...")
        # Give uvicorn a moment to finish in-flight requests, then exit.
        # os._exit is required here because uvicorn's signal handlers can
        # prevent a clean sys.exit when SSE connections are open.
        time.sleep(2)
        os._exit(0)


def main() -> None:
    run_web()


if __name__ == "__main__":
    main()
