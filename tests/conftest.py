"""Shared test configuration — ensure project root is on sys.path and stub heavy deps."""
import pytest
import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Some third-party wheels ship a top-level `tests` package into site-packages. A regular package always beats this
# directory (which has no __init__.py, on purpose: several tests import siblings by bare name), so
# `from tests.test_x import ...` resolved to the wrong package and collection stopped. Bind `tests` to this directory.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_tests_mod = sys.modules.get("tests")
if _tests_mod is None or _TESTS_DIR not in [os.path.abspath(p) for p in getattr(_tests_mod, "__path__", [])]:
    _tests_mod = types.ModuleType("tests")
    _tests_mod.__path__ = [_TESTS_DIR]
    sys.modules["tests"] = _tests_mod

def _has_module(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except (ImportError, ValueError):
        return False


# Stub optional dependencies only when they are not installed. Do not replace
# real FastAPI/Starlette/Pydantic modules: route tests import their subpackages.
for mod_name in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.types", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "sqlalchemy.sql.sqltypes", "bcrypt", "pyotp",
    "httpx", "fastapi", "fastapi.responses", "fastapi.routing",
    "starlette", "starlette.responses", "starlette.middleware", "starlette.middleware.base",
    "pydantic",
]:
    if mod_name not in sys.modules and not _has_module(mod_name):
        sys.modules[mod_name] = MagicMock()

if "src.database" not in sys.modules:
    _db = types.ModuleType("src.database")
    _db.SessionLocal = MagicMock()
    _db.ModelEndpoint = MagicMock()
    sys.modules["src.database"] = _db


@pytest.fixture(autouse=True)
def _no_live_job_feed(monkeypatch):
    """The standing tick checks a public job feed once a day; in tests that feed is empty, never the network."""
    try:
        from src.foundation import freelance
    except Exception:
        return
    monkeypatch.setattr(freelance, "_default_fetch", lambda: b"[]")
