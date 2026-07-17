import os
from contextlib import AbstractAsyncContextManager

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from feishu_generation_agent.config import Settings


def open_checkpointer(
    settings: Settings,
) -> AbstractAsyncContextManager[AsyncSqliteSaver]:
    path = settings.checkpoint_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(path))
