from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base


class Database:
    def __init__(self, url: str):
        filename = make_url(url).database
        if filename and filename != ":memory:":
            Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url, connect_args={"timeout": 10})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async def initialize(self):
        async with self.engine.connect() as conn:
            # API and worker serialize this additive upgrade of existing databases.
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            await conn.run_sync(Base.metadata.create_all)
            columns = (await conn.exec_driver_sql("PRAGMA table_info(jobs)")).all()
            if "publish_requested" not in {column[1] for column in columns}:
                await conn.exec_driver_sql(
                    "ALTER TABLE jobs ADD COLUMN publish_requested BOOLEAN NOT NULL DEFAULT 0"
                )
            await conn.commit()

    async def close(self):
        await self.engine.dispose()
