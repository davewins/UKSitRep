import logging
import os

from aiohttp import web

from .config import Config
from .server import make_app

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
cfg = Config.from_env()
web.run_app(make_app(cfg), host=cfg.host, port=cfg.port, access_log=None, print=None)
