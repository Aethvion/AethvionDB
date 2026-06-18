"""Shared pytest fixtures for AethvionDB. All use isolated tmp_path dirs."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


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
