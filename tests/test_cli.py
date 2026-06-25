"""
tests/test_cli.py
The aethviondb CLI (P1-7). Commands run against the session temp data dir
(set in conftest); main() returns the process exit code.
"""
from __future__ import annotations

from aethviondb import cli
from aethviondb.db_registry import resolve_db_root


def _writer(db: str):
    from aethviondb.entity_writer import EntityWriter
    from aethviondb.name_index import NameIndex
    root = resolve_db_root(db)
    return EntityWriter(entities_dir=root / "entities",
                        index=NameIndex(index_path=root / "name_index.json"))


def test_version(capsys):
    assert cli.main(["version"]) == 0
    assert "aethviondb" in capsys.readouterr().out


def test_init_creates_db(db_name):
    assert cli.main(["init", db_name]) == 0
    assert (resolve_db_root(db_name) / "entities").exists()


def test_backup_list_restore(db_name, capsys):
    cli.main(["init", db_name])
    w = _writer(db_name)
    e, _ = w.create("Keep", entity_type="concept")

    assert cli.main(["backup", db_name, "--label", "snap"]) == 0
    out = capsys.readouterr().out
    assert "Created backup" in out
    bid = out.split("Created backup ")[1].split()[0]

    assert cli.main(["backups", db_name]) == 0
    assert bid in capsys.readouterr().out

    w.delete(e["id"], soft=False)                  # remove after backup
    assert _writer(db_name).get(e["id"]) is None

    assert cli.main(["restore", db_name, bid]) == 0
    assert _writer(db_name).get(e["id"]) is not None   # back after restore


def test_validate_exit_codes(db_name):
    cli.main(["init", db_name])
    w = _writer(db_name)
    assert cli.main(["validate", db_name]) == 0     # empty/clean → 0

    e, _ = w.create("Broken", entity_type="concept")
    w.update(e["id"], {"sections": {"relations": [{"kind": "depends_on", "target_id": "ws_ghost"}]}})
    assert cli.main(["validate", db_name]) == 1     # has an error → 1


def test_restore_missing_returns_1(db_name, capsys):
    cli.main(["init", db_name])
    assert cli.main(["restore", db_name, "no_such_backup"]) == 1
    assert "Error" in capsys.readouterr().out
