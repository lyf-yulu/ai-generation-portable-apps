import asyncio
from pathlib import Path

import pytest

from feishu_generation_agent.storage.planner_prompts import PlannerPromptStore
from feishu_generation_agent.web.app import create_app


PROMPT = "优先保持人物造型一致，并按镜头拆分任务。"


async def test_open_creates_table_and_missing_user_returns_none(tmp_path: Path) -> None:
    store = await PlannerPromptStore.open(tmp_path / "business.sqlite3")
    try:
        assert await store.get("user-a") is None
    finally:
        await store.close()


async def test_first_save_creates_version_one(tmp_path: Path) -> None:
    store = await PlannerPromptStore.open(tmp_path / "business.sqlite3")
    try:
        profile = await store.save(
            portal_user_id="user-a",
            username="甲",
            prompt_text=PROMPT,
        )

        assert profile.version == 1
        assert profile.portal_user_id == "user-a"
        assert profile.username == "甲"
        assert profile.prompt_text == PROMPT
        assert profile.created_at.endswith("+00:00")
        assert profile.updated_at.endswith("+00:00")
    finally:
        await store.close()


async def test_second_save_for_same_user_increments_version(tmp_path: Path) -> None:
    store = await PlannerPromptStore.open(tmp_path / "business.sqlite3")
    try:
        await store.save(portal_user_id="user-a", username="甲", prompt_text=PROMPT)

        profile = await store.save(
            portal_user_id="user-a",
            username="乙",
            prompt_text="用明确的分镜描述动作和镜头运动。",
        )

        assert profile.version == 2
        assert profile.username == "乙"
    finally:
        await store.close()


async def test_two_connections_to_same_database_increment_versions_safely(
    tmp_path: Path,
) -> None:
    path = tmp_path / "business.sqlite3"
    first = await PlannerPromptStore.open(path)
    second = await PlannerPromptStore.open(path)
    try:
        one, two = await asyncio.gather(
            first.save(portal_user_id="user-a", username="甲", prompt_text=PROMPT),
            second.save(
                portal_user_id="user-a",
                username="甲",
                prompt_text="突出镜头衔接和节奏。",
            ),
        )

        assert sorted((one.version, two.version)) == [1, 2]
        assert (await first.get("user-a")).version == 2
    finally:
        await first.close()
        await second.close()


async def test_user_profiles_are_independent_and_delete_only_removes_requested_user(
    tmp_path: Path,
) -> None:
    store = await PlannerPromptStore.open(tmp_path / "business.sqlite3")
    try:
        await store.save(portal_user_id="user-a", username="甲", prompt_text=PROMPT)
        profile_b = await store.save(
            portal_user_id="user-b", username="乙", prompt_text="先定义关键视觉元素。"
        )

        assert profile_b.version == 1
        assert await store.delete("user-a") is True
        assert await store.get("user-a") is None
        assert (await store.get("user-b")).prompt_text == "先定义关键视觉元素。"
        assert await store.delete("user-a") is False
    finally:
        await store.close()


@pytest.mark.parametrize("prompt_text", ["", " \t\n", "文" * 20_001])
async def test_save_rejects_blank_and_overlong_prompt_text(
    tmp_path: Path, prompt_text: str
) -> None:
    store = await PlannerPromptStore.open(tmp_path / "business.sqlite3")
    try:
        with pytest.raises(ValueError):
            await store.save(
                portal_user_id="user-a", username="甲", prompt_text=prompt_text
            )
    finally:
        await store.close()


async def test_reopening_same_database_preserves_profile(tmp_path: Path) -> None:
    path = tmp_path / "business.sqlite3"
    store = await PlannerPromptStore.open(path)
    try:
        await store.save(portal_user_id="user-a", username="甲", prompt_text=PROMPT)
    finally:
        await store.close()

    reopened = await PlannerPromptStore.open(path)
    try:
        profile = await reopened.get("user-a")
        assert profile is not None
        assert profile.portal_user_id == "user-a"
        assert profile.prompt_text == PROMPT
        assert profile.version == 1
    finally:
        await reopened.close()


class _Runtime:
    async def close(self) -> None:
        pass


async def test_app_does_not_close_an_injected_prompt_store(tmp_path: Path) -> None:
    store = await PlannerPromptStore.open(tmp_path / "business.sqlite3")
    app = create_app(runtime=_Runtime(), planner_prompt_store=store)
    try:
        async with app.router.lifespan_context(app):
            assert app.state.planner_prompt_store is store

        profile = await store.save(
            portal_user_id="user-a", username="甲", prompt_text=PROMPT
        )
        assert profile.version == 1
    finally:
        await store.close()
