"""
conftest.py — test environment bootstrap for the brave-pos backend test suite.

server.py reads MONGO_URL and DB_NAME from os.environ at *import time* and
immediately creates an AsyncIOMotorClient.  Tests that do ``import server``
inside their bodies will fail with KeyError unless both env-vars are present
and the Motor client construction is patched out.

This conftest satisfies both requirements without touching server.py.
"""
import os
import sys
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# 1. Inject required env-vars before any test body runs
#    (conftest module-level code executes during collection, before any test)
# ---------------------------------------------------------------------------
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "testdb")


# ---------------------------------------------------------------------------
# 2. Patch AsyncIOMotorClient so server.py's module-level import doesn't
#    attempt a real TCP connection.  We do this with a permanent sys.modules
#    patch that stays in place for the entire pytest session.
# ---------------------------------------------------------------------------
_motor_mock = MagicMock()
_motor_mock.return_value.__getitem__ = MagicMock(return_value=MagicMock())

# Only patch if server hasn't been imported yet
if "server" not in sys.modules:
    _motor_patch = patch(
        "motor.motor_asyncio.AsyncIOMotorClient",
        _motor_mock,
    )
    _motor_patch.start()
    # We intentionally never call _motor_patch.stop() — the mock must stay
    # active for the full session because server.py is only imported once.
