# P1 — 频道层打通（长连接 / 收私聊 / 落图 / 回消息）

**前置**：`docs/plans/2026-08-13-super-agent-01-architecture.md`
**范围**：只做「机器人能收到私聊消息、把参考图落盘、回一条确认」。**不做**需求澄清、不做 plan、不进 LangGraph。

## 验收标准

在飞书私聊里给机器人发「帮我做张图」+ 3 张图片，机器人回复：

```
收到：文本 1 条，参考图 3 张（已存档）
会话 ID：sess_xxxx
```

且 `data/runs/sess_xxxx/inputs/` 下有 3 个按 sha256 命名的文件；重复推送同一条消息不会重复落盘。

---

## 决策记录（本期锁定）

| 项 | 决定 | 理由 |
|---|---|---|
| 事件传输 | WebSocket 长连接 | 服务机无公网 IP；出站连接，免内网穿透 |
| 会话键 | `(open_id, chat_id)` | 同一人在不同会话互不干扰 |
| 图片落盘位置 | `FileStore.save_input(session_id, ...)` | 复用已有原子写 + sha256 寻址；`session_id` 当 segment 用，P3 起 run 时直接把这些路径转成 `MediaAsset`，不搬文件 |
| 群聊 | **不响应**（本期） | P1 只单聊，`drop_self_sent` + 非 P2P 直接丢弃 |
| 去重 | 已有 `bot_ingress` 表 | `accept_ingress()` 返回 False 即已处理过 |
| 进程 | 独立 launchd，**不挂 `com.ai-portal`** | 照抄 `com.feishu-output-sync` 的隔离；Portal 重启不打断会话 |

---

## 新增文件

```
src/feishu_generation_agent/
  channel/
    __init__.py
    config.py        # 频道相关 Settings 读取 + FeishuChannel 构造
    inbound.py       # InboundMessage → InboundTurn（归一化 + 私聊过滤）
    media.py         # CachedResource → FileStore 落盘
    session_ids.py   # (open_id, chat_id) → session_id
  cli/
    bot.py           # 常驻进程入口
tests/unit/
  test_bot_channel_config.py
  test_bot_inbound.py
  test_bot_media.py
deploy/
  com.feishu-super-agent.plist
```

---

## Step 1 — 配置项

### 1.1 先写测试

`tests/unit/test_bot_channel_config.py`：

```python
from feishu_generation_agent.config import Settings


def test_bot_settings_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.lark_bot_enabled is False
    assert settings.bot_dm_only is True
    assert settings.bot_media_cache_dir.as_posix().endswith("bot-media-cache")
    # 会话澄清模型独立于 vision 模型：读图仍归 ClaudeVisionAnalyzer + planner
    assert settings.bot_session_model == "claude-opus-4-7"


def test_bot_session_model_is_independent_of_vision_model() -> None:
    settings = Settings(_env_file=None, claude_model="claude-sonnet-4-6")
    assert settings.claude_model == "claude-sonnet-4-6"
    assert settings.bot_session_model == "claude-opus-4-7"
```

### 1.2 实现

`config.py` 的 `Settings` 里，`lark_bot_enabled` 那行下面追加：

```python
    lark_bot_enabled: bool = False          # 已存在，本期开始真正被读取
    bot_dm_only: bool = True                # P1 只响应私聊
    bot_media_cache_dir: Path = Path("data/bot-media-cache")
    bot_session_db_path: Path = Path("data/bot-sessions.sqlite3")
    # 会话澄清层专用；不复用 claude_model（那是 vision 的，读图链路不变）
    bot_session_model: str = "claude-opus-4-7"
```

同时在 `ensure_paths()` 里把 `bot_media_cache_dir` 加进创建列表。

### 1.3 验证

```bash
cd feishu-generation-agent && uv run pytest tests/unit/test_bot_channel_config.py -q
```

期望：`2 passed`

---

## Step 2 — FeishuChannel 构造（只构造，不连接）

### 2.1 先写测试

追加到 `tests/unit/test_bot_channel_config.py`：

```python
import pytest
from feishu_generation_agent.channel.config import build_channel, BotNotConfigured


def test_build_channel_requires_credentials() -> None:
    with pytest.raises(BotNotConfigured):
        build_channel(Settings(_env_file=None))


def test_build_channel_uses_ws_transport(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        lark_app_id="cli_test",
        lark_app_secret="secret_test",
        lark_bot_enabled=True,
        bot_media_cache_dir=tmp_path / "cache",
    )
    channel = build_channel(settings)
    assert channel.config.transport.kind == "ws"
    assert channel.config.transport.auto_reconnect is True
    assert channel.config.transport.keepalive.enabled is True
    assert channel.config.inbound.drop_self_sent is True
```

### 2.2 实现

`channel/config.py`：

```python
from __future__ import annotations

from lark_channel import FeishuChannel
from lark_channel.channel import ChannelConfig, KeepaliveConfig, MediaCacheConfig, TransportConfig

from feishu_generation_agent.config import Settings


class BotNotConfigured(RuntimeError):
    """飞书机器人凭据或开关缺失。"""


def build_channel(settings: Settings) -> FeishuChannel:
    if not settings.lark_bot_enabled:
        raise BotNotConfigured("LARK_BOT_ENABLED 未开启")
    if not settings.lark_app_id or settings.lark_app_secret is None:
        raise BotNotConfigured("缺少 LARK_APP_ID / LARK_APP_SECRET")

    config = ChannelConfig()
    config.transport = TransportConfig(
        kind="ws",
        auto_reconnect=True,
        keepalive=KeepaliveConfig(enabled=True),
    )
    config.inbound.drop_self_sent = True
    config.inbound.media_capabilities.__dict__.setdefault  # 保持默认能力集
    config.media_cache = MediaCacheConfig(
        enabled=True,
        root_dir=settings.bot_media_cache_dir,
    )
    return FeishuChannel(
        app_id=settings.lark_app_id,
        app_secret=settings.lark_app_secret.get_secret_value(),
        config=config,
    )
```

> ⚠️ `ChannelConfig` 上 `media_cache` 的确切属性名要以 `lark_channel/channel/config.py` 的 `ChannelConfig` 定义为准；实现时先
> `python -c "from lark_channel.channel import ChannelConfig; print(ChannelConfig().__dict__.keys())"` 打一遍字段名再落笔。
> **不要传任何采样参数**（`temperature` 等）到后续 Claude 调用 —— Opus 4.7 官方 API 会 400，当前中转只是容忍（已实测）。

### 2.3 验证

```bash
uv run python -c "from lark_channel.channel import ChannelConfig; print(sorted(ChannelConfig().__dict__.keys()))"
uv run pytest tests/unit/test_bot_channel_config.py -q
```

期望：先打印出真实字段名，再 `4 passed`

---

## Step 3 — 会话 ID

### 3.1 先写测试

`tests/unit/test_bot_inbound.py`：

```python
from feishu_generation_agent.channel.session_ids import session_id_for


def test_session_id_is_stable_and_scoped() -> None:
    a = session_id_for(open_id="ou_1", chat_id="oc_1")
    b = session_id_for(open_id="ou_1", chat_id="oc_1")
    c = session_id_for(open_id="ou_1", chat_id="oc_2")
    d = session_id_for(open_id="ou_2", chat_id="oc_1")
    assert a == b
    assert a != c and a != d
    assert a.startswith("sess_")
    # 必须能安全当作文件系统 segment（FileStore._validate_segment）
    assert a.replace("_", "").isalnum()
```

### 3.2 实现

`channel/session_ids.py`：

```python
import hashlib


def session_id_for(*, open_id: str, chat_id: str) -> str:
    digest = hashlib.sha256(f"{open_id}\x00{chat_id}".encode()).hexdigest()
    return f"sess_{digest[:24]}"
```

### 3.3 验证

```bash
uv run pytest tests/unit/test_bot_inbound.py -q
```

期望：`1 passed`

---

## Step 4 — 入站归一化 + 私聊过滤

### 4.1 先写测试

追加到 `tests/unit/test_bot_inbound.py`：

```python
from lark_channel.channel.types import (
    Conversation, Identity, InboundMessage, ResourceDescriptor, TextContent,
)
from feishu_generation_agent.channel.inbound import InboundTurn, normalize_turn


def _msg(chat_type: str = "p2p", resources=None) -> InboundMessage:
    return InboundMessage(
        id="om_1",
        create_time=1,
        conversation=Conversation(chat_id="oc_1", chat_type=chat_type),
        sender=Identity(open_id="ou_1", display_name="张三"),
        content=TextContent(text="帮我做张图"),
        content_text="帮我做张图",
        body_text="帮我做张图",
        resources=list(resources or []),
    )


def test_p2p_message_is_accepted() -> None:
    turn = normalize_turn(_msg(), dm_only=True)
    assert isinstance(turn, InboundTurn)
    assert turn.open_id == "ou_1"
    assert turn.text == "帮我做张图"
    assert turn.session_id.startswith("sess_")
    assert turn.dedupe_id == "om_1"


def test_group_message_is_dropped_in_p1() -> None:
    assert normalize_turn(_msg(chat_type="group"), dm_only=True) is None


def test_resources_are_carried_through() -> None:
    resources = [
        ResourceDescriptor(type="image", file_key="img_1"),
        ResourceDescriptor(type="image", file_key="img_2"),
    ]
    turn = normalize_turn(_msg(resources=resources), dm_only=True)
    assert [r.file_key for r in turn.resources] == ["img_1", "img_2"]
```

### 4.2 实现

`channel/inbound.py`：

```python
from __future__ import annotations

from dataclasses import dataclass, field

from lark_channel.channel.types import InboundMessage, ResourceDescriptor

from .session_ids import session_id_for


@dataclass(frozen=True, slots=True)
class InboundTurn:
    session_id: str
    open_id: str
    chat_id: str
    message_id: str
    dedupe_id: str
    text: str
    display_name: str | None = None
    resources: list[ResourceDescriptor] = field(default_factory=list)


def normalize_turn(msg: InboundMessage, *, dm_only: bool) -> InboundTurn | None:
    if dm_only and msg.conversation.chat_type != "p2p":
        return None
    if msg.sender.is_bot:
        return None
    open_id = msg.sender.open_id
    chat_id = msg.conversation.chat_id
    if not open_id or not chat_id:
        return None
    return InboundTurn(
        session_id=session_id_for(open_id=open_id, chat_id=chat_id),
        open_id=open_id,
        chat_id=chat_id,
        message_id=msg.id,
        dedupe_id=msg.id,
        text=(msg.body_text or msg.content_text or "").strip(),
        display_name=msg.sender.display_name,
        resources=list(msg.resources),
    )
```

### 4.3 验证

```bash
uv run pytest tests/unit/test_bot_inbound.py -q
```

期望：`4 passed`

---

## Step 5 — 媒体落盘

### 5.1 先写测试

`tests/unit/test_bot_media.py`：

```python
import pytest
from lark_channel.channel.types import CachedResource, ResourceDescriptor

from feishu_generation_agent.channel.media import stage_resources
from feishu_generation_agent.storage.files import FileStore


class _FakeChannel:
    def __init__(self, results): self._results = results; self.calls = []
    async def resolve_resources_to_cache(self, *, message_id, resources):
        self.calls.append((message_id, [r.file_key for r in resources]))
        return self._results


@pytest.mark.asyncio
async def test_cached_images_are_written_to_file_store(tmp_path) -> None:
    src = tmp_path / "a.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    channel = _FakeChannel([
        CachedResource(decision="cached", path=src, mime_type="image/png", size=src.stat().st_size),
    ])
    store = FileStore(data_dir=tmp_path / "data", outputs_dir=tmp_path / "out")

    staged = await stage_resources(
        channel=channel, store=store, session_id="sess_abc",
        message_id="om_1", resources=[ResourceDescriptor(type="image", file_key="img_1")],
    )

    assert len(staged) == 1
    assert staged[0].mime_type == "image/png"
    assert staged[0].local_path.exists()
    assert staged[0].sha256


@pytest.mark.asyncio
async def test_rejected_resources_are_skipped(tmp_path) -> None:
    channel = _FakeChannel([CachedResource(decision="rejected", reason="too_large")])
    store = FileStore(data_dir=tmp_path / "data", outputs_dir=tmp_path / "out")
    staged = await stage_resources(
        channel=channel, store=store, session_id="sess_abc",
        message_id="om_1", resources=[ResourceDescriptor(type="file", file_key="f_1")],
    )
    assert staged == []
```

> `FileStore(...)` 的真实构造参数以 `storage/files.py` 为准，实现时先看一眼 `__init__`。

### 5.2 实现

`channel/media.py`：

```python
from __future__ import annotations

from lark_channel.channel.types import ResourceDescriptor

from feishu_generation_agent.storage.files import FileStore, StoredFile


async def stage_resources(
    *,
    channel,
    store: FileStore,
    session_id: str,
    message_id: str,
    resources: list[ResourceDescriptor],
) -> list[StoredFile]:
    if not resources:
        return []
    cached = await channel.resolve_resources_to_cache(
        message_id=message_id, resources=resources
    )
    staged: list[StoredFile] = []
    for item in cached:
        if item.decision != "cached" or item.path is None:
            continue
        content = item.path.read_bytes()
        staged.append(
            store.save_input(session_id, item.path.name, content)
        )
    return staged
```

### 5.3 验证

```bash
uv run pytest tests/unit/test_bot_media.py -q
```

期望：`2 passed`

---

## Step 6 — 装配 + 幂等 + 回复

`channel/runner.py`（本步无独立单测，靠 Step 7 的真实联调验收）：

```python
async def handle_message(msg, *, channel, settings, repository, store) -> None:
    turn = normalize_turn(msg, dm_only=settings.bot_dm_only)
    if turn is None:
        return
    accepted = await repository.accept_ingress(
        dedupe_id=turn.dedupe_id, kind="im_message",
        command={"session_id": turn.session_id, "open_id": turn.open_id},
    )
    if not accepted:          # 重连回灌 / 飞书重推
        return
    try:
        staged = await stage_resources(
            channel=channel, store=store, session_id=turn.session_id,
            message_id=turn.message_id, resources=turn.resources,
        )
        await channel.reply(msg, {"text":
            f"收到：文本 {1 if turn.text else 0} 条，参考图 {len(staged)} 张（已存档）\n"
            f"会话 ID：{turn.session_id}"
        })
        await repository.finish_ingress(turn.dedupe_id, status="completed",
                                        result={"staged": len(staged)})
    except Exception as exc:
        await repository.finish_ingress(turn.dedupe_id, status="failed",
                                        result={"error": str(exc)})
        raise
```

`cli/bot.py`：

```python
import asyncio, signal
from feishu_generation_agent.config import Settings
from feishu_generation_agent.channel.config import build_channel


def main() -> None:
    asyncio.run(_run())


async def _run() -> None:
    settings = Settings()
    settings.ensure_paths()
    channel = build_channel(settings)
    # repository / store 复用 bootstrap 里的打开方式
    channel.on("message", lambda msg: handle_message(msg, channel=channel, ...))
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await channel.start_background(timeout=30.0)
    await stop.wait()
    await channel.stop_background()
```

`pyproject.toml` 的 `[project.scripts]` 追加：

```toml
feishu-super-agent-bot = "feishu_generation_agent.cli.bot:main"
```

---

## Step 7 — 真实联调

**前置**：架构文档第 6 节的飞书后台 checklist 全部做完并**重新发版**。

```bash
cd feishu-generation-agent
echo "LARK_BOT_ENABLED=true" >> .env
uv sync
uv run feishu-super-agent-bot
```

期望日志出现连接就绪。然后在飞书**私聊**机器人：

1. 只发文字「你好」→ 回 `收到：文本 1 条，参考图 0 张`
2. 发文字 + 3 张图 → 回 `参考图 3 张`，且：

```bash
ls data/runs/sess_*/inputs/
```

期望：3 个文件
3. 在群里 @机器人 → **无任何回复**（P1 不响应群聊）

---

## Step 8 — 独立 launchd

`deploy/com.feishu-super-agent.plist`：Label `com.feishu-super-agent`，`KeepAlive=true`，`RunAtLoad=true`，解释器用 `.venv` 里的 Python，`WorkingDirectory` 指向 `feishu-generation-agent`，日志写 `~/Library/Logs/feishu-super-agent.log`。

```bash
cp deploy/com.feishu-super-agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.feishu-super-agent.plist
launchctl list | grep feishu-super-agent
```

期望：有 PID 且退出码 0。**不要**把它挂进 `com.ai-portal`。

---

## 本期不做

- 需求澄清（Opus 4.7 多轮追问）→ P2
- 进 LangGraph、出 plan、卡片审批 → P3
- 产出回传 → P4
- 群聊、并发、超时兜底 → P5

## 回归检查

```bash
uv run pytest -q
```

期望：现有全部测试仍为绿。P1 不改动 `graph/`、`integrations/`、`web/` 任何文件 —— 只新增 `channel/` 和 `cli/bot.py`，以及 `config.py` 的追加字段。
