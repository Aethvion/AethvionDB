"""
aethviondb/cli.py
Command-line interface for AethvionDB.

    aethviondb serve [--host H --port P --no-browser]
    aethviondb init <db>
    aethviondb backup <db> [--label L]
    aethviondb backups <db>
    aethviondb restore <db> <backup_id>
    aethviondb validate <db> [--json]      # exit code 1 if any entity has errors
    aethviondb version

All commands operate on the data directory (AETHVIONDB_DATA_DIR, default
~/.aethvion/aethviondb). ``main()`` returns the process exit code.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from aethviondb import __version__


def _db_root(db: str) -> Path:
    from aethviondb.db_registry import resolve_db_root
    return resolve_db_root(db)


def _writer(db: str):
    from aethviondb.entity_writer import EntityWriter
    from aethviondb.name_index import NameIndex
    root = _db_root(db)
    return EntityWriter(entities_dir=root / "entities",
                        index=NameIndex(index_path=root / "name_index.json"))


def cmd_serve(args) -> int:
    import os
    if args.host:
        os.environ["AETHVIONDB_HOST"] = args.host
    if args.port:
        os.environ["AETHVIONDB_PORT"] = str(args.port)
    if args.no_browser:
        os.environ["AETHVIONDB_OPEN_BROWSER"] = "0"
    from aethviondb.server import main as serve_main
    serve_main()
    return 0


def cmd_init(args) -> int:
    root = _db_root(args.db)
    (root / "entities").mkdir(parents=True, exist_ok=True)
    (root / "chunks").mkdir(parents=True, exist_ok=True)
    print(f"Initialized database {args.db!r} at {root}")
    return 0


def cmd_backup(args) -> int:
    from aethviondb.backup import create_backup
    meta = create_backup(_db_root(args.db), args.db, label=args.label or "")
    print(f"Created backup {meta['backup_id']} "
          f"({meta['entity_count']} entities, {meta['size_bytes']} bytes)")
    return 0


def cmd_backups(args) -> int:
    from aethviondb.backup import list_backups
    backups = list_backups(_db_root(args.db))
    if not backups:
        print("No backups.")
        return 0
    for m in backups:
        print(f"{m['backup_id']}\t{m.get('entity_count', '?')} entities\t{m.get('created', '')}")
    return 0


def cmd_restore(args) -> int:
    from aethviondb.backup import restore_backup
    try:
        rep = restore_backup(_db_root(args.db), args.backup_id)
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1
    print(f"Restored {args.backup_id}: {rep['entity_count']} entities")
    return 0


def cmd_validate(args) -> int:
    from aethviondb.validator import Validator
    summary = Validator(writer=_writer(args.db)).summary()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(f"Entities: {summary['total_entities']}  "
              f"Clean: {summary['clean']}  With errors: {summary['with_errors']}")
        print(f"Errors: {summary['total_errors']}  Warnings: {summary['total_warnings']}")
        for e in summary.get("entities_with_errors", [])[:20]:
            msgs = "; ".join(i["message"] for i in e["issues"])
            print(f"  ✗ {e['name']}: {msgs}")
    return 1 if summary["with_errors"] else 0   # nonzero exit for CI gating


def cmd_reindex(args) -> int:
    res = _writer(args.db).reindex()
    print(f"Reindexed {args.db}: {res['entities']} entities, {res['index_entries']} index entries")
    return 0


def cmd_version(args) -> int:
    print(f"aethviondb {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aethviondb",
        description="AethvionDB — an agent-first knowledge database.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the HTTP server + dashboard")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--no-browser", action="store_true", help="don't open a browser")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("init", help="create an empty database")
    s.add_argument("db")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("backup", help="create a backup")
    s.add_argument("db")
    s.add_argument("--label", default="")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("backups", help="list backups")
    s.add_argument("db")
    s.set_defaults(func=cmd_backups)

    s = sub.add_parser("restore", help="restore a backup (replaces db contents)")
    s.add_argument("db")
    s.add_argument("backup_id")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("validate", help="run consistency checks (exit 1 on errors)")
    s.add_argument("db")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("reindex", help="rebuild snapshot + name index from the entity files")
    s.add_argument("db")
    s.set_defaults(func=cmd_reindex)

    s = sub.add_parser("version", help="print the version")
    s.set_defaults(func=cmd_version)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
