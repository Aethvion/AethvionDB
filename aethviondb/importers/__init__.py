"""
AethvionDB importers — convert external databases into AethvionDB (Layer 2).

One small adapter per source type, registered in IMPORTERS. Add a new source by
writing an adapter (subclass BaseImporter) and registering it here — nothing in
the Layer-1 core changes.
"""
from .base import BaseImporter, ImportSummary, bulk_write
from .sqlite import SQLiteImporter

IMPORTERS = {
    "sqlite": SQLiteImporter,
}

__all__ = ["BaseImporter", "ImportSummary", "SQLiteImporter", "IMPORTERS", "bulk_write"]
