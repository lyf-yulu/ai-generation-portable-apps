# Seedance 轮询窗口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Seedance 的默认异步轮询窗口提升到约 15 分钟，同时保留环境变量覆盖能力。

**Architecture:** 只修改 `Settings.provider_poll_max_attempts` 的默认值；轮询节点继续使用既有的 `provider_poll_interval_seconds × provider_poll_max_attempts` 组合。测试夹具已经显式设置短窗口，不改变其执行时间。

**Tech Stack:** Python 3.12、Pydantic Settings、pytest。

## Global Constraints

- 默认轮询间隔保持 1 秒。
- 默认最大轮询次数为 900，对应约 15 分钟。
- `PROVIDER_POLL_INTERVAL_SECONDS` 和 `PROVIDER_POLL_MAX_ATTEMPTS` 必须继续覆盖默认值。
- 不能更改历史超时任务的恢复行为、Seedance 提交参数或飞书写入逻辑。

---

### Task 1: 默认轮询窗口

**Files:**

- Modify: `src/feishu_generation_agent/config.py:50-51`
- Test: `tests/unit/test_config.py`

**Interfaces:**

- Produces: `Settings().provider_poll_max_attempts == 900`，且 `Settings().provider_poll_interval_seconds == 1.0`。
- Preserves: 从环境变量读取这两个字段的既有 Pydantic Settings 行为。

- [ ] **Step 1: Write the failing test**

```python
def test_provider_polling_defaults_to_fifteen_minutes(monkeypatch):
    monkeypatch.delenv("PROVIDER_POLL_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("PROVIDER_POLL_MAX_ATTEMPTS", raising=False)
    settings = Settings()
    assert settings.provider_poll_interval_seconds == 1.0
    assert settings.provider_poll_max_attempts == 900
```

- [ ] **Step 2: Run RED**

Run: `/Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -m pytest tests/unit/test_config.py -q`

Expected: FAIL because the current default is 120 attempts.

- [ ] **Step 3: Implement the minimal setting change**

```python
provider_poll_max_attempts: int = Field(default=900, ge=1, le=10_000)
```

- [ ] **Step 4: Run GREEN**

Run: `/Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -m pytest tests/unit/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/config.py tests/unit/test_config.py
git commit -m "fix(agent): extend Seedance polling window"
```

### Task 2: 回归验证和本地服务交接

**Files:**

- Modify: `docs/superpowers/plans/2026-07-22-seedance-poll-window.md`

- [ ] **Step 1: Run the complete automated suite**

Run: `/Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -m pytest -q`

Expected: all tests pass; the fixture's explicit 4-attempt setting keeps graph tests fast.

- [ ] **Step 2: Verify the production process receives the default**

Run: `/Users/260413a/ai-generation-portable-apps/feishu-generation-agent/.venv/bin/python -c 'from feishu_generation_agent.config import Settings; print(Settings().provider_poll_max_attempts)'`

Expected: `900`.

- [ ] **Step 3: Commit the completed plan**

```bash
git add docs/superpowers/plans/2026-07-22-seedance-poll-window.md
git commit -m "docs(agent): record poll window verification"
```

