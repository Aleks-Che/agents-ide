import sqlite3
from pathlib import Path

import portalocker
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Engine, create_engine, event

from agents_ide.config import Settings

SCHEMA_REVISION = "0014_council_safety"


def create_database(settings: Settings) -> Engine:
    engine = create_engine(
        URL.create("sqlite", database=str(settings.database_path)),
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def configure(connection: sqlite3.Connection, _: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")

    return engine


def migrate(settings: Settings) -> None:
    # Launcher/API/worker can start simultaneously; migration has one owner.
    with portalocker.Lock(str(settings.data_dir / "runtime/migrate.lock"), timeout=30):
        engine = create_database(settings)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
                connection.commit()
                config = Config()
                config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        finally:
            engine.dispose()


def check_database(engine: Engine) -> bool:
    with engine.connect() as connection:
        return (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
            == SCHEMA_REVISION
        )
