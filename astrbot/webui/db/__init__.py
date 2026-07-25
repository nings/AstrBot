"""Independent SQLite-backed database layer for the AstrBot WebUI module.

This package owns its own :class:`sqlmodel.MetaData` (see
``astrbot.webui.db.models.WebUiBaseModel``) and its own SQLite file
(see ``astrbot.webui.db.engine``). It never touches AstrBot's core tables
or core migrations.
"""

from __future__ import annotations
