"""Smoke tests: verify every module imports without error.

These catch missing imports, syntax errors, and top-level NameErrors
that would otherwise only surface at runtime.  The run.py entry point
historically broke due to a missing ``import logging`` — this test
class exists to prevent that class of bug from recurring.
"""

import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

# ── Collect every .py module in the taktis package ──────────────

_PKG_ROOT = Path(__file__).resolve().parent.parent / "taktis"


def _all_module_names():
    """Yield dotted module names for every .py file under taktis_engine/."""
    for info in pkgutil.walk_packages(
        path=[str(_PKG_ROOT)],
        prefix="taktis.",
    ):
        yield info.name


_MODULES = sorted(_all_module_names())


# ── Parametrised import test ──────────────────────────────────────────

@pytest.mark.parametrize("module_name", _MODULES)
def test_module_imports_cleanly(module_name: str):
    """Importing ``{module_name}`` must not raise."""
    importlib.import_module(module_name)


# ── Entry-point script (run.py) ──────────────────────────────────────

def test_run_py_is_importable():
    """run.py must parse and execute at module level without error.

    This is the exact bug that slipped through: a missing ``import logging``
    caused a NameError on ``class _JSONFormatter(logging.Formatter)``.
    """
    run_py = Path(__file__).resolve().parent.parent / "run.py"
    assert run_py.exists(), "run.py not found at project root"

    # compile() catches SyntaxError; exec() catches NameError / ImportError
    source = run_py.read_text(encoding="utf-8")
    code = compile(source, str(run_py), "exec")

    # Execute in an isolated namespace so side-effects don't leak.
    # We only care that top-level definitions succeed — not that main() runs.
    ns = {"__name__": "_import_test", "__file__": str(run_py)}
    exec(code, ns)

    # Verify key symbols are defined
    assert "run_web" in ns, "run_web() function must be defined"
    assert "_JSONFormatter" in ns, "_JSONFormatter class must be defined"
