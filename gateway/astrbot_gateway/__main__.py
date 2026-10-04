import logging
import os

import uvicorn

from .app import create_app
from .config import Settings

logging.basicConfig(level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO"))
uvicorn.run(
    create_app(Settings.from_env()),
    host=os.environ.get("GATEWAY_HOST", "0.0.0.0"),
    port=int(os.environ.get("GATEWAY_PORT", "8080")),
)
