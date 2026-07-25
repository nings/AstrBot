"""SSO/OAuth provider adapters for the AstrBot Portal module (Phase 5b).

``base.py`` defines the shared :class:`~astrbot.portal.services.sso.base.SsoProvider`
protocol and the :class:`~astrbot.portal.services.sso.base.VerifiedIdentity`
shape every adapter resolves a callback into -- read its module docstring
first, it is the load-bearing design decision for this whole package.

``oidc.py`` is the only adapter this file set implements: a generic OpenID
Connect authorization-code client on ``pyjwt``/``httpx`` (no new dependency).
WeCom (企业微信) and Feishu (飞书) adapters are **out of this file set's
ownership** -- see ``astrbot/portal/services/sso_service.py``'s
``notes_for_integrator`` for the exact contract whoever adds
``sso/wecom.py``/``sso/feishu.py`` must implement and where to register them.

Deliberately no re-exports here beyond the package docstring: import
``astrbot.portal.services.sso.base`` / ``.oidc`` directly, matching this
module's existing style elsewhere (``astrbot.portal.services`` itself does
not re-export its submodules either).
"""

from __future__ import annotations
