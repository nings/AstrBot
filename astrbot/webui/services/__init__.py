"""Service layer for the AstrBot WebUI module.

``core_facade`` is the sole capability-wall crossing point into AstrBot core
and the operator-facing dashboard service layer -- see its module docstring.
Every other file under ``astrbot/webui/**`` (including ``session_service``
in this same package) must go through it rather than importing core/
dashboard internals directly.
"""

from __future__ import annotations
