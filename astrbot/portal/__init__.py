"""AstrBot Portal module.

Independent multi-user end-user chat frontend/backend, layered on top of
AstrBot core via ``astrbot.portal.services.core_facade`` (the sole capability
wall import point, see the module's design doc for details).

This package intentionally has no import-time side effects: submodules must
be imported explicitly (e.g. ``astrbot.portal.db.engine``,
``astrbot.portal.config``) by whichever code wires the module into the
dashboard app.
"""

from __future__ import annotations
