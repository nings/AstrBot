from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Plan:
    """Per-plan limits and the AstrBot config profile used for its users.

    Attributes:
        config_id: AstrBot config profile ID passed to `/api/v1/chat`. None uses
            AstrBot's routing table (usually the default profile).
        daily_chat_limit: Max chat requests per user per UTC day. None means
            unlimited.
        storage_mb: Max total size of files a user may keep in the gateway.
    """

    config_id: str | None = None
    daily_chat_limit: int | None = 100
    storage_mb: int = 200


@dataclass(frozen=True)
class Settings:
    astrbot_base_url: str
    astrbot_api_key: str
    jwt_secret: str
    jwt_audience: str | None = None
    jwt_issuer: str | None = None
    data_dir: Path = Path("data")
    max_file_mb: int = 20
    max_doc_chars: int = 100_000
    file_ttl_days: int = 30
    plans: dict[str, Plan] = field(default_factory=lambda: {"default": Plan()})
    default_plan: str = "default"

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings from `GATEWAY_*` / `ASTRBOT_*` environment variables.

        Returns:
            The parsed settings.

        Raises:
            ValueError: If a required variable is missing or a plan is invalid.
        """
        missing = [
            name
            for name in ("ASTRBOT_API_KEY", "GATEWAY_JWT_SECRET")
            if not os.environ.get(name)
        ]
        if missing:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")

        plans = {"default": Plan()}
        if raw_plans := os.environ.get("GATEWAY_PLANS"):
            plans = {name: Plan(**conf) for name, conf in json.loads(raw_plans).items()}
        for name, plan in plans.items():
            if plan.daily_chat_limit is not None and plan.daily_chat_limit < 1:
                raise ValueError(f"Plan {name}: daily_chat_limit must be >= 1")
            if plan.storage_mb < 1:
                raise ValueError(f"Plan {name}: storage_mb must be >= 1")

        default_plan = os.environ.get("GATEWAY_DEFAULT_PLAN", "default")
        if default_plan not in plans:
            raise ValueError(
                f"GATEWAY_DEFAULT_PLAN not in GATEWAY_PLANS: {default_plan}"
            )

        return cls(
            astrbot_base_url=os.environ.get(
                "ASTRBOT_BASE_URL", "http://127.0.0.1:6185"
            ).rstrip("/"),
            astrbot_api_key=os.environ["ASTRBOT_API_KEY"],
            jwt_secret=os.environ["GATEWAY_JWT_SECRET"],
            jwt_audience=os.environ.get("GATEWAY_JWT_AUDIENCE") or None,
            jwt_issuer=os.environ.get("GATEWAY_JWT_ISSUER") or None,
            data_dir=Path(os.environ.get("GATEWAY_DATA_DIR", "data")),
            max_file_mb=int(os.environ.get("GATEWAY_MAX_FILE_MB", "20")),
            max_doc_chars=int(os.environ.get("GATEWAY_MAX_DOC_CHARS", "100000")),
            file_ttl_days=int(os.environ.get("GATEWAY_FILE_TTL_DAYS", "30")),
            plans=plans,
            default_plan=default_plan,
        )
