"""Shared pytest fixtures for AethvionDB. All use isolated tmp_path dirs."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Point the whole test session at a throwaway data dir BEFORE aethviondb.config
# is imported, so HTTP/API tests (which resolve db roots from AETHVIONDB_DATA_DIR)
# never touch the user's real ~/.aethvion. Library tests use explicit tmp dirs.
os.environ.setdefault("AETHVIONDB_DATA_DIR", tempfile.mkdtemp(prefix="aethviondb_test_"))

import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def pytest_addoption(parser):
    parser.addoption("--runslow", action="store_true", default=False,
                     help="run slow stress tests (large-DB scale checks)")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: large-scale stress test (opt in with --runslow)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--runslow"):
        return
    skip = pytest.mark.skip(reason="needs --runslow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture()
def client():
    """A FastAPI TestClient over a fresh app (shares the session temp data dir)."""
    from fastapi.testclient import TestClient
    from aethviondb.server import create_app
    return TestClient(create_app())


@pytest.fixture()
def db_name():
    """A unique database name per test, so tests don't collide in the shared dir."""
    import uuid
    return "t_" + uuid.uuid4().hex[:10]


@pytest.fixture()
def db_dir(tmp_path: Path) -> Path:
    d = tmp_path / "aethviondb" / "test_db"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def name_index(db_dir: Path):
    from aethviondb.name_index import NameIndex
    return NameIndex(index_path=db_dir / "name_index.json")


@pytest.fixture()
def entity_writer(db_dir: Path, name_index):
    from aethviondb.entity_writer import EntityWriter
    entities_dir = db_dir / "entities"
    entities_dir.mkdir(parents=True, exist_ok=True)
    return EntityWriter(entities_dir=entities_dir, index=name_index)
