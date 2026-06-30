"""
AethvionDB — an agent-first knowledge database engine.

Every entity (person, place, event, concept, module, decision…) shares an
identical JSON envelope, stored one file per entity (the durable source of
truth). A name-to-ID index prevents duplicates; fast access is served from
snapshots and purpose-baked views derived from the raw entries.

Multiple databases are supported — each is an independent directory under the
configured data dir, with its own ``entities/`` folder and ``name_index.json``.

Public surface
--------------
    from aethviondb import NameIndex, EntityWriter, Validator, ContentDistiller
"""

from .name_index import NameIndex
from .entity_writer import EntityWriter, VersionConflictError
from .entity_schema import SCHEMA_VERSION, migrate
from .validator import Validator
from .distiller import ContentDistiller
from .client import AethvionClient, AethvionError

__version__ = "1.0.0rc1"
__all__ = [
    "NameIndex",
    "EntityWriter",
    "VersionConflictError",
    "SCHEMA_VERSION",
    "migrate",
    "Validator",
    "ContentDistiller",
    "AethvionClient",
    "AethvionError",
    "__version__",
]
