"""Apply database migrations at startup (``alembic upgrade head``)."""

import os

from alembic import command
from alembic.config import Config
from loguru import logger

ALEMBIC_INI = os.path.join(os.path.dirname(os.path.realpath(__file__)), "alembic.ini")


def upgrade_database() -> None:
    config = Config(ALEMBIC_INI)
    # 保留应用自己的日志配置，避免 alembic.ini 的 logging 段覆盖 loguru 输出。
    config.attributes["configure_logger"] = False
    logger.info("applying database migrations")
    command.upgrade(config, "head")
    logger.info("database schema is up to date")
