from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from feishu_generation_agent.config import Settings


@asynccontextmanager
async def open_checkpointer(
    settings: Settings,
) -> AsyncIterator[AsyncSqliteSaver]:
    path = settings.checkpoint_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    serializer = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=None,
    )
    async with aiosqlite.connect(str(path)) as connection:
        yield AsyncSqliteSaver(connection, serde=serializer)
