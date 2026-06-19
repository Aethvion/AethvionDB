"""
aethviondb/importers/sqlite.py
Import a SQLite database into AethvionDB.

The mapping is intentionally literal — what you read here is exactly what it does:

  table                      -> entity kind        (entity type stays "other")
  row                        -> one entity         (ID is stable: table + primary key)
  primary key                -> entity identity
  foreign key                -> a typed relation   (edge to the referenced row)
  a name/title/label column  -> the entity name    (else "<table> #<pk>")
  a description/notes column  -> the entity summary
  every other column         -> a property

Foreign keys are where the graph comes from: a relational schema already encodes
one, so importing it produces a graph an agent can traverse — no AI required.
The database is opened read-only; the source file is never modified.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

from aethviondb._utils import get_logger
from .base import BaseImporter, deterministic_id, make_entity

logger = get_logger(__name__)

_NAME_COLS    = ("name", "title", "label")
_SUMMARY_COLS = ("description", "summary", "notes", "body", "content", "comment")


class SQLiteImporter(BaseImporter):
    source_type = "sqlite"

    def __init__(self, source: str):
        super().__init__(source)
        self.path = Path(source)
        if not self.path.exists():
            raise FileNotFoundError(f"SQLite file not found: {source}")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def _tables(self, con) -> list[str]:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]

    def _columns(self, con, table) -> list[sqlite3.Row]:
        return con.execute(f'PRAGMA table_info("{table}")').fetchall()

    def _pk_cols(self, con, table) -> list[str]:
        info = self._columns(con, table)
        pks = [r["name"] for r in sorted(info, key=lambda r: r["pk"]) if r["pk"]]
        return pks or [r["name"] for r in info]   # no PK → identity = all columns

    def _fks(self, con, table) -> list[dict]:
        # PRAGMA foreign_key_list: id, seq, table, from, to, on_update, on_delete, match
        return [{"from": r["from"], "to": r["to"], "table": r["table"]}
                for r in con.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()]

    @staticmethod
    def _relation_kind(fk_col: str, target_table: str) -> str:
        c = fk_col.lower()
        if c.endswith("id") and len(c) > 2:
            return c[:-2].rstrip("_")          # ArtistId -> "artist"
        return target_table.lower() or "references"

    def preview(self) -> dict:
        con = self._connect()
        try:
            tables, est_e, est_r = [], 0, 0
            for t in self._tables(con):
                rows = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                cols = [r["name"] for r in self._columns(con, t)]
                fks = self._fks(con, t)
                tables.append({
                    "name": t, "rows": rows, "columns": cols,
                    "foreign_keys": [f"{f['from']} → {f['table']}.{f['to']}" for f in fks],
                })
                est_e += rows
                est_r += rows * len(fks)
            return {"source_type": "sqlite", "source": str(self.path),
                    "tables": tables, "est_entities": est_e, "est_relations": est_r}
        finally:
            con.close()

    def iter_entities(self) -> Iterator[dict]:
        con = self._connect()
        try:
            for table in self._tables(con):
                pk_cols  = self._pk_cols(con, table)
                fks      = self._fks(con, table)
                fk_from  = {f["from"] for f in fks}
                cols     = [r["name"] for r in self._columns(con, table)]
                name_col = (next((c for c in cols if c.lower() in _NAME_COLS), None)
                            or next((c for c in cols if "name" in c.lower() or "title" in c.lower()), None))
                sum_col  = next((c for c in cols if c.lower() in _SUMMARY_COLS), None)

                for row in con.execute(f'SELECT * FROM "{table}"'):
                    d = dict(row)
                    pk_vals = [d.get(c) for c in pk_cols]
                    eid = deterministic_id(table, *pk_vals)

                    if name_col is not None and d.get(name_col) is not None:
                        name = str(d[name_col])
                    else:
                        name = f"{table} #" + "-".join(str(v) for v in pk_vals)
                    summary = str(d[sum_col]) if sum_col and d.get(sum_col) is not None else ""

                    relations = []
                    for f in fks:
                        val = d.get(f["from"])
                        if val is None:
                            continue
                        relations.append({
                            "kind": self._relation_kind(f["from"], f["table"]),
                            "target_id": deterministic_id(f["table"], val),
                            "note": "",
                        })

                    props = {}
                    for c, v in d.items():
                        if c in pk_cols or c in fk_from or c == name_col or c == sum_col or v is None:
                            continue
                        if isinstance(v, (str, int, float, bool)):
                            props[c] = v
                        elif isinstance(v, bytes):
                            props[c] = f"<blob {len(v)} bytes>"
                        else:
                            props[c] = str(v)

                    yield make_entity(entity_id=eid, name=name, kind=table, source="sqlite",
                                      summary=summary, properties=props, relations=relations)
        finally:
            con.close()
