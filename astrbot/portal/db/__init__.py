"""Independent SQLite-backed database layer for the AstrBot Portal module.

This package owns its own :class:`sqlmodel.MetaData` (see
``astrbot.portal.db.models.PortalBaseModel``) and its own SQLite file
(see ``astrbot.portal.db.engine``). It never touches AstrBot's core tables
or core migrations.
"""

from __future__ import annotations
