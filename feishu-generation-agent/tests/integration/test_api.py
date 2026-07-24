import asyncio
import copy
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables.config import set_config_context
from langsmith import tracing_context
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from PIL import Image

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.document import (
    RequirementRequest,
    build_planning_prompt_snapshot,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import ApprovalDecision
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.runtime import (
    GraphRuntime,
    RunNotFound,
    RunValidationError,
)
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.planner_prompts import PlannerPromptStore
from feishu_generation_agent.storage.repository import Repository
from feishu_generation_agent.web.app import create_app
from feishu_generation_agent.integrations.planner import planner_system_prompt
from feishu_generation_agent.cli import smoke


def _task(task_id: str = "task-1") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_type": "image_to_video",
        "title": "纸船漂流",
        "source_block_ids": ["story-1"],
        "user_intent": "生成纸船漂流视频",
        "prompt": "蓝色纸船连续漂向远处",
        "negative_constraints": [],
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "16:9",
        "image_size": None,
        "duration": 10,
        "resolution": "720p",
        "generate_audio": False,
        "output_count": 1,
        "confidence": 0.9,
        "assumptions": [],
        "warnings": [],
        "blocking_issues": [],
    }


def _image_task(task_id: str = "task-2") -> dict[str, Any]:
    task = _task(task_id)
    task.update(
        task_type="image_to_image",
        title="纸船插画",
        prompt="蓝色纸船静置在河面",
        image_size="2K",
        duration=None,
        resolution=None,
        generate_audio=None,
    )
    return task


class FakeApprovalGraph:
    def __init__(self, repository: Repository, image_path: Path) -> None:
        self.repository = repository
        self.image_path = image_path
        self.states: dict[str, dict[str, Any]] = {}
        self.resume_calls = 0
        self.fail_initial = False
        self.resume_error: AgentError | None = None
        self.resume_started = asyncio.Event()
        self.resume_release: asyncio.Event | None = None

    @staticmethod
    def _thread_id(config: dict[str, Any]) -> str:
        return config["configurable"]["thread_id"]

    def _interrupt(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "__interrupt__": [
                SimpleNamespace(
                    value={
                        "action": "review_plan",
                        "run_id": state["run_id"],
                        "thread_id": state["thread_id"],
                        "draft_plan": state["draft_plan"],
                        "validation_issues": [],
                    }
                )
            ]
        }

    async def ainvoke(
        self,
        value: dict[str, Any] | Command | None,
        *,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        thread_id = self._thread_id(config)
        if isinstance(value, dict):
            if self.fail_initial:
                self.states[thread_id] = {**value, "status": "running"}
                raise RuntimeError("fictional-secret-background-failure")
            run_id = value["run_id"]
            await self.repository.append_event(
                run_id, "validate_plan", "started", "Plan validation started"
            )
            await self.repository.append_event(
                run_id,
                "validate_plan",
                "completed",
                "Plan validation completed",
            )
            asset = {
                "asset_id": "asset-1",
                "source_block_id": "image-1",
                "origin": "feishu",
                "file_token": None,
                "local_path": str(self.image_path),
                "mime_type": "image/png",
                "size": self.image_path.stat().st_size,
                "sha256": "safe-test-hash",
                "width": 16,
                "height": 16,
                "download_error": None,
            }
            plan = {
                "tasks": [_task(), _image_task()],
                "document_summary": "纸船图片与视频",
            }
            state = {
                **value,
                "status": "waiting_approval",
                "document_id": "doc-test",
                "document_title": "纸船需求",
                "document_revision": 7,
                "source_revision": 7,
                "draft_plan": plan,
                "task_plan": plan,
                "media_assets": [asset],
                "vision_descriptions": [
                    {
                        "asset_id": "asset-1",
                        "subjects": ["蓝色纸船"],
                        "scene": "河面",
                        "style": "插画",
                        "composition": "居中",
                        "characters": [],
                        "actions": ["漂流"],
                        "visible_text": [],
                        "colors": ["蓝色"],
                        "probable_role": "主体参考",
                        "uncertainties": [],
                    }
                ],
                "approval_decision": None,
                "approved_tasks": [],
                "validation_issues": [],
            }
            self.states[thread_id] = state
            return self._interrupt(state)

        state = self.states[thread_id]
        if value is None:
            return self._interrupt(state)

        self.resume_calls += 1
        self.resume_started.set()
        if self.resume_release is not None:
            await self.resume_release.wait()
        if self.resume_error is not None:
            raise self.resume_error
        decision = value.resume
        if decision["action"] == "cancel":
            state.update(status="cancelled", approval_decision=decision)
            return state
        if decision["action"] == "reject":
            state.update(status="waiting_approval", approval_decision=decision)
            return self._interrupt(state)

        selected = set(decision["selected_task_ids"])
        tasks = decision.get("tasks") or state["draft_plan"]["tasks"]
        approved = [task for task in tasks if task["task_id"] in selected]
        state.update(
            status="approved",
            approval_decision=decision,
            approved_tasks=approved,
        )
        return state

    async def aget_state(self, config: dict[str, Any]) -> SimpleNamespace:
        state = self.states.get(self._thread_id(config), {})
        interrupts = ()
        next_nodes: tuple[str, ...] = ()
        if state.get("status") == "waiting_approval":
            interrupts = (
                SimpleNamespace(value=self._interrupt(state)["__interrupt__"][0].value),
            )
            next_nodes = ("human_approval",)
        return SimpleNamespace(
            values=state,
            next=next_nodes,
            tasks=(SimpleNamespace(interrupts=interrupts),) if interrupts else (),
        )

    async def aupdate_state(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
        *,
        as_node: str | None = None,
    ) -> None:
        del as_node
        self.states.setdefault(self._thread_id(config), {}).update(values)


@asynccontextmanager
async def _environment(tmp_path: Path, *, bitable_service=None):
    settings = Settings(
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
        max_download_bytes=1024 * 1024,
    )
    settings.ensure_paths()
    image_path = settings.data_dir / "source.png"
    image_path.write_bytes(b"fake-source-image")
    repository = await Repository.open(settings.business_db_path)
    prompt_store = await PlannerPromptStore.open(
        tmp_path / "environment-planner-prompts.sqlite3"
    )
    file_store = FileStore(
        settings.data_dir,
        settings.outputs_dir,
        max_bytes=settings.max_download_bytes,
    )
    graph = FakeApprovalGraph(repository, image_path)
    runtime = GraphRuntime(
        graph=graph,
        repository=repository,
        file_store=file_store,
        settings=settings,
    )
    app = create_app(
        runtime=runtime,
        bitable_service=bitable_service,
        planner_prompt_store=prompt_store,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                yield client, runtime, graph, repository
    finally:
        await prompt_store.close()
        await repository.close()


@asynccontextmanager
async def _prompt_environment(tmp_path: Path, *, expose_store: bool = False):
    store = await PlannerPromptStore.open(tmp_path / "planner-prompts.sqlite3")
    app = create_app(planner_prompt_store=store)
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                yield (client, store) if expose_store else client
    finally:
        await store.close()


_USER_A_HEADERS = {"X-Portal-User-Id": "user-a", "X-Username": "%E7%94%B2"}
_USER_B_HEADERS = {"X-Portal-User-Id": "user-b", "X-Username": "%E4%B9%99"}


async def test_portal_reserved_prime_identity_is_rejected(tmp_path: Path) -> None:
    async with _prompt_environment(tmp_path) as client:
        response = await client.get(
            "/api/planner-prompt",
            headers={"X-Portal-User-Id": "prime-local"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Portal 用户身份无效"


async def test_direct_local_planner_prompt_is_read_only_prime(tmp_path: Path) -> None:
    async with _prompt_environment(tmp_path) as client:
        response = await client.get("/api/planner-prompt")
        put_response = await client.put(
            "/api/planner-prompt", json={"prompt_text": "不应保存"}
        )
        delete_response = await client.delete("/api/planner-prompt")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "prime",
        "editable": False,
        "prompt_text": planner_system_prompt(),
        "version": 0,
        "source": "prime",
    }
    assert put_response.status_code == 403
    assert delete_response.status_code == 403


async def test_portal_planner_prompt_isolated_by_header_identity(tmp_path: Path) -> None:
    first_prompt = "优先保持人物造型一致，并按镜头拆分任务。"
    second_prompt = "让镜头运动、光线和节奏保持连续。"
    async with _prompt_environment(tmp_path) as client:
        inherited = await client.get("/api/planner-prompt", headers=_USER_A_HEADERS)
        saved_first = await client.put(
            "/api/planner-prompt",
            headers=_USER_A_HEADERS,
            json={"prompt_text": first_prompt},
        )
        saved_second = await client.put(
            "/api/planner-prompt",
            headers=_USER_A_HEADERS,
            json={"prompt_text": second_prompt},
        )
        user_b = await client.get("/api/planner-prompt", headers=_USER_B_HEADERS)
        deleted = await client.delete(
            "/api/planner-prompt", headers=_USER_A_HEADERS
        )
        user_b_after_delete = await client.get(
            "/api/planner-prompt", headers=_USER_B_HEADERS
        )

    assert inherited.json() == {
        "mode": "prime",
        "editable": True,
        "prompt_text": planner_system_prompt(),
        "version": 0,
        "source": "prime",
    }
    assert saved_first.json() == {
        "mode": "personal",
        "editable": True,
        "prompt_text": first_prompt,
        "version": 1,
        "source": "personal",
    }
    assert saved_second.json() == {
        "mode": "personal",
        "editable": True,
        "prompt_text": second_prompt,
        "version": 2,
        "source": "personal",
    }
    assert user_b.json() == {
        "mode": "prime",
        "editable": True,
        "prompt_text": planner_system_prompt(),
        "version": 0,
        "source": "prime",
    }
    assert deleted.json() == inherited.json()
    assert user_b_after_delete.json() == user_b.json()


async def test_planner_prompt_uses_only_portal_header_user_id(tmp_path: Path) -> None:
    prompt = "只应保存给请求头中的用户。"
    async with _prompt_environment(tmp_path, expose_store=True) as (client, store):
        saved = await client.put(
            "/api/planner-prompt?portal_user_id=user-b",
            headers=_USER_A_HEADERS,
            json={"prompt_text": prompt, "portal_user_id": "user-b"},
        )
        user_a = await client.get("/api/planner-prompt", headers=_USER_A_HEADERS)
        user_b = await client.get("/api/planner-prompt", headers=_USER_B_HEADERS)
        profile = await store.get("user-a")

    assert saved.status_code == 200
    assert user_a.json()["prompt_text"] == prompt
    assert user_b.json()["source"] == "prime"
    assert "user-a" not in user_a.text
    assert "user-b" not in user_a.text
    assert profile is not None
    assert profile.username == "甲"


@pytest.mark.parametrize("prompt_text", [" \t\n", "文" * 20_001])
async def test_planner_prompt_rejects_blank_and_overlong_values(
    tmp_path: Path, prompt_text: str
) -> None:
    async with _prompt_environment(tmp_path) as client:
        response = await client.put(
            "/api/planner-prompt",
            headers=_USER_A_HEADERS,
            json={"prompt_text": prompt_text},
        )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Portal-User-Id": " ", "X-Username": "%E7%94%B2"},
        {"X-Portal-User-Id": "user-a", "X-Username": "%ZZ"},
        {"X-Portal-User-Id": "user-a" * 100, "X-Username": "%E7%94%B2"},
    ],
)
async def test_planner_prompt_rejects_malformed_or_overlong_identity(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    async with _prompt_environment(tmp_path) as client:
        response = await client.get("/api/planner-prompt", headers=headers)

    assert response.status_code == 400


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 403),
        ({"X-Portal-User-Id": " ", "X-Username": "%E7%94%B2"}, 400),
    ],
    ids=["anonymous", "malformed-identity"],
)
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"prompt_text": " \t\n"},
        {"prompt_text": "secret-prompt-" + "文" * 20_000},
    ],
    ids=["missing-body", "blank-prompt", "overlong-prompt"],
)
async def test_planner_prompt_put_checks_identity_before_invalid_body(
    tmp_path: Path,
    headers: dict[str, str],
    expected_status: int,
    payload: dict[str, str] | None,
) -> None:
    async with _prompt_environment(tmp_path) as client:
        request_kwargs = {"headers": headers}
        if payload is not None:
            request_kwargs["json"] = payload
        response = await client.put("/api/planner-prompt", **request_kwargs)

    assert response.status_code == expected_status
    assert "secret-prompt-" not in response.text


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 403),
        ({"X-Portal-User-Id": " ", "X-Username": "%E7%94%B2"}, 400),
    ],
    ids=["anonymous", "malformed-identity"],
)
async def test_planner_prompt_delete_checks_identity(
    tmp_path: Path,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    async with _prompt_environment(tmp_path) as client:
        response = await client.delete("/api/planner-prompt", headers=headers)

    assert response.status_code == expected_status


async def _wait_for_status(
    client: httpx.AsyncClient,
    run_id: str,
    expected: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    for _ in range(100):
        response = await client.get(
            f"/api/runs/{run_id}", headers=headers
        )
        if response.status_code == 200 and response.json()["status"] == expected:
            return response.json()
        await asyncio.sleep(0.01)
    raise AssertionError(f"run did not reach {expected}")


async def test_run_view_exposes_safe_chinese_validation_field_paths(
    tmp_path: Path,
) -> None:
    raw_prompt = "English prompt that must not be exposed"
    message = "以下字段必须包含中文主体说明：tasks[0].prompt"
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, repository
        created = await client.post(
            "/api/runs",
            json={"source_url": "https://tenant.feishu.cn/docx/chinese"},
        )
        run_id = created.json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        graph.resume_error = AgentError(
            ErrorDetail(
                category=ErrorCategory.VALIDATION,
                message=message,
                technical_detail="safe validation error",
                retryable=False,
            )
        )

        response = await client.post(
            f"/api/runs/{run_id}/decision",
            json={"action": "approve", "selected_task_ids": ["task-1"]},
        )
        view = await client.get(f"/api/runs/{run_id}")

    assert response.status_code == 422
    assert "tasks[0].prompt" in response.text
    assert raw_prompt not in response.text
    assert view.status_code == 200
    assert view.json()["last_error"]["message"] == message
    assert raw_prompt not in view.text


async def test_run_and_reference_routes_hide_user_a_run_from_user_b(
    tmp_path: Path,
) -> None:
    async with _environment(tmp_path) as (
        client,
        runtime,
        graph,
        repository,
    ):
        del runtime, graph
        created = await client.post(
            "/api/runs",
            headers=_USER_A_HEADERS,
            json={"source_url": "https://acme.feishu.cn/docx/owned"},
        )
        run_id = created.json()["run_id"]
        await _wait_for_status(
            client,
            run_id,
            "waiting_approval",
            headers=_USER_A_HEADERS,
        )
        owned = await repository.get_run(
            run_id, owner_user_id="user-a"
        )
        assert owned is not None

        responses = [
            await client.get(
                f"/api/runs/{run_id}", headers=_USER_B_HEADERS
            ),
            await client.post(
                f"/api/runs/{run_id}/decision",
                headers=_USER_B_HEADERS,
                json={"action": "cancel"},
            ),
            await client.post(
                f"/api/runs/{run_id}/retry-delivery",
                headers=_USER_B_HEADERS,
            ),
            await client.post(
                f"/api/runs/{run_id}/references",
                headers=_USER_B_HEADERS,
                data={
                    "task_id": "task-1",
                    "role": "reference_image",
                    "order": "2",
                },
                files={
                    "file": ("replacement.png", _png_bytes(), "image/png")
                },
            ),
            await client.patch(
                f"/api/runs/{run_id}/tasks/task-1/references",
                headers=_USER_B_HEADERS,
                json={
                    "references": [
                        {
                            "asset_id": "asset-1",
                            "role": "reference_image",
                            "order": 1,
                        }
                    ]
                },
            ),
            await client.delete(
                f"/api/runs/{run_id}/tasks/task-1/references/asset-1",
                headers=_USER_B_HEADERS,
            ),
            await client.get(
                f"/api/runs/{run_id}/references/asset-1/content",
                headers=_USER_B_HEADERS,
            ),
            await client.delete(
                f"/api/runs/{run_id}", headers=_USER_B_HEADERS
            ),
        ]

        assert [response.status_code for response in responses] == [404] * len(
            responses
        )
        assert (
            await repository.get_run(
                run_id, owner_user_id="user-a"
            )
        ) is not None


async def test_normal_run_approve_and_delete_with_production_service_enabled(
    tmp_path: Path,
) -> None:
    class ProductionWithoutBindings:
        async def sync_once(
            self, run_id: str, *, owner_user_id: str
        ) -> None:
            raise RunNotFound("多维表格运行不存在")

        async def is_production_run(
            self, run_id: str, *, owner_user_id: str
        ) -> bool:
            return False

        async def validate_approval(
            self, run_id: str, *, owner_user_id: str
        ) -> None:
            raise AssertionError("normal run reached production validation")

        async def delete_run(
            self, run_id: str, *, owner_user_id: str
        ) -> None:
            raise AssertionError("normal run reached production deletion")

        async def close(self) -> None:
            pass

    production = ProductionWithoutBindings()
    async with _environment(
        tmp_path, bitable_service=production
    ) as (client, runtime, graph, repository):
        del runtime, graph, repository
        first = await client.post(
            "/api/runs",
            headers=_USER_A_HEADERS,
            json={"source_url": "https://acme.feishu.cn/docx/normal-approve"},
        )
        first_run_id = first.json()["run_id"]
        await _wait_for_status(
            client,
            first_run_id,
            "waiting_approval",
            headers=_USER_A_HEADERS,
        )
        approved = await client.post(
            f"/api/runs/{first_run_id}/decision",
            headers=_USER_A_HEADERS,
            json={
                "action": "approve",
                "selected_task_ids": ["task-1"],
            },
        )

        second = await client.post(
            "/api/runs",
            headers=_USER_A_HEADERS,
            json={"source_url": "https://acme.feishu.cn/docx/normal-delete"},
        )
        second_run_id = second.json()["run_id"]
        await _wait_for_status(
            client,
            second_run_id,
            "waiting_approval",
            headers=_USER_A_HEADERS,
        )
        deleted = await client.delete(
            f"/api/runs/{second_run_id}", headers=_USER_A_HEADERS
        )

    assert approved.status_code == 202
    assert deleted.status_code == 200


async def test_clone_run_for_approval_reuses_approved_draft_without_generation(
    tmp_path: Path,
) -> None:
    async with _environment(tmp_path) as (client, runtime, graph, _repository):
        created = await client.post(
            "/api/runs", json={"source_url": "https://tenant.feishu.cn/docx/source"}
        )
        original_run_id = created.json()["run_id"]
        original = await _wait_for_status(client, original_run_id, "waiting_approval")
        graph.states[original["thread_id"]]["approved_tasks"] = [
            original["approval"]["tasks"][0]
        ]

        cloned_run_id = await runtime.clone_run_for_approval(
            original_run_id,
            RequirementRequest(source_url=original["source_url"]),
            run_id="rerun-1",
            thread_id="rerun-thread-1",
        )
        cloned = await _wait_for_status(client, cloned_run_id, "waiting_approval")

    assert cloned_run_id == "rerun-1"
    assert cloned["approval"]["tasks"] == [original["approval"]["tasks"][0]]
    assert cloned["approval"]["selected_task_ids"] == ["task-1"]
    assert graph.resume_calls == 0


async def test_clone_prefers_approved_plan_when_approved_tasks_are_missing(
    tmp_path: Path,
) -> None:
    async with _environment(tmp_path) as (client, runtime, graph, _repository):
        created = await client.post(
            "/api/runs",
            json={"source_url": "https://tenant.feishu.cn/docx/prefer-approved"},
        )
        original_run_id = created.json()["run_id"]
        original = await _wait_for_status(
            client, original_run_id, "waiting_approval"
        )
        approved = {
            "tasks": [copy.deepcopy(original["approval"]["tasks"][0])],
            "document_summary": original["approval"]["document_summary"],
            "excluded_assets": [],
        }
        source_state = graph.states[original["thread_id"]]
        source_state["approved_plan"] = approved
        source_state["approved_tasks"] = []

        cloned_run_id = await runtime.clone_run_for_approval(
            original_run_id,
            RequirementRequest(source_url=original["source_url"]),
            run_id="rerun-prefer-approved",
            thread_id="rerun-prefer-approved-thread",
        )
        cloned = await _wait_for_status(
            client, cloned_run_id, "waiting_approval"
        )

    assert cloned["approval"]["tasks"] == approved["tasks"]
    assert cloned["approval"]["selected_task_ids"] == ["task-1"]
    assert graph.resume_calls == 0


async def test_clone_run_fails_closed_without_source_prompt_snapshot(
    tmp_path: Path,
) -> None:
    async with _environment(tmp_path) as (client, runtime, graph, _repository):
        created = await client.post(
            "/api/runs",
            json={"source_url": "https://tenant.feishu.cn/docx/source"},
        )
        original_run_id = created.json()["run_id"]
        original = await _wait_for_status(
            client, original_run_id, "waiting_approval"
        )
        state = graph.states[original["thread_id"]]
        state["approved_tasks"] = [original["approval"]["tasks"][0]]
        state.pop("planning_prompt")

        with pytest.raises(RunValidationError, match="提示词快照"):
            await runtime.clone_run_for_approval(
                original_run_id,
                RequirementRequest(source_url=original["source_url"]),
                run_id="rerun-no-prompt",
                thread_id="rerun-no-prompt-thread",
            )


async def test_clone_run_for_approval_requires_a_previously_approved_task(
    tmp_path: Path,
) -> None:
    async with _environment(tmp_path) as (client, runtime, _graph, _repository):
        created = await client.post(
            "/api/runs", json={"source_url": "https://tenant.feishu.cn/docx/source"}
        )
        original_run_id = created.json()["run_id"]
        original = await _wait_for_status(client, original_run_id, "waiting_approval")

        with pytest.raises(Exception, match="原运行没有已批准任务"):
            await runtime.clone_run_for_approval(
                original_run_id,
                RequirementRequest(source_url=original["source_url"]),
                run_id="rerun-without-approval",
                thread_id="rerun-without-approval-thread",
            )


async def test_clone_run_for_approval_initializes_a_real_langgraph_checkpoint(
    fake_services: GraphServices,
) -> None:
    graph = build_graph(fake_services, InMemorySaver())
    runtime = GraphRuntime(
        graph=graph,
        repository=fake_services.repository,
        file_store=fake_services.file_store,
        settings=fake_services.settings,
        delivery_writer=fake_services.delivery_writer,
    )
    try:
        original_run_id = await runtime.start_run(
            RequirementRequest(source_url="https://tenant.feishu.cn/docx/clone-source"),
            run_id="clone-original",
            thread_id="clone-original-thread",
        )
        for _ in range(100):
            original = await runtime.get_run_view(original_run_id)
            if original["status"] == "waiting_approval":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("original run did not reach approval")
        await graph.aupdate_state(
            {"configurable": {"thread_id": original["thread_id"]}},
            {"approved_tasks": [original["approval"]["tasks"][0]]},
            as_node="validate_plan",
        )

        cloned_run_id = await runtime.clone_run_for_approval(
            original_run_id,
            RequirementRequest(source_url="https://tenant.feishu.cn/docx/clone-source"),
            run_id="clone-real",
            thread_id="clone-real-thread",
        )
        cloned = await runtime.get_run_view(cloned_run_id)
    finally:
        await runtime.close()

    assert cloned["status"] == "waiting_approval"
    assert cloned["approval"]["tasks"] == original["approval"]["tasks"]
    assert fake_services.planner.plan_calls == 1


async def _complete_run_with_approval_edits(
    runtime: GraphRuntime,
    graph: Any,
    services: GraphServices,
    *,
    run_id: str,
    thread_id: str,
) -> dict[str, Any]:
    source_url = "https://tenant.feishu.cn/docx/clone-approved-source"
    await runtime.start_run(
        RequirementRequest(source_url=source_url),
        run_id=run_id,
        thread_id=thread_id,
    )
    for _ in range(100):
        source = await runtime.get_run_view(run_id)
        if source["status"] == "waiting_approval":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("source run did not reach approval")

    uploaded = await runtime.add_reference(
        run_id,
        task_id="task-video",
        role="reference_image",
        order=2,
        filename="second-reference.png",
        content=_png_bytes((80, 170, 120)),
    )
    edited_view = await runtime.get_run_view(run_id)
    edited_task = copy.deepcopy(edited_view["approval"]["tasks"][0])
    edited_task.update(
        prompt="纸船在雨夜河面缓慢漂流，镜头持续向前推进。",
        aspect_ratio="9:16",
        duration=5,
        resolution="1080p",
        reference_images=[
            {
                "asset_id": uploaded["asset_id"],
                "role": "reference_image",
                "order": 1,
            }
        ],
    )
    await runtime.resume_run(
        run_id,
        ApprovalDecision(
            action="approve",
            selected_task_ids=["task-video"],
            tasks=[edited_task],
        ),
    )
    source = await runtime.get_run_view(run_id)
    assert source["status"] == "succeeded"
    snapshot = await graph.aget_state(
        {"configurable": {"thread_id": source["thread_id"]}}
    )
    approved = copy.deepcopy(snapshot.values["approved_plan"])
    assert approved["tasks"][0]["prompt"] == edited_task["prompt"]
    assert (
        approved["tasks"][0]["reference_images"]
        == edited_task["reference_images"]
    )
    assert approved["tasks"][0]["aspect_ratio"] == "9:16"
    assert approved["tasks"][0]["duration"] == 5
    assert approved["tasks"][0]["resolution"] == "1080p"
    assert approved["excluded_assets"] == [
        {"asset_id": "asset-1", "reason": "用户在审批中移除"}
    ]
    assert services.video_generator.submit_calls == 1
    return approved


async def _assert_cloned_approval_matches(
    runtime: GraphRuntime,
    graph: Any,
    services: GraphServices,
    approved: dict[str, Any],
    *,
    source_run_id: str,
    clone_run_id: str,
    clone_thread_id: str,
) -> None:
    image_submit_calls = services.image_generator.submit_calls
    submit_calls = services.video_generator.submit_calls
    delivery_calls = services.delivery_writer.deliver_calls
    planner_calls = services.planner.plan_calls

    await runtime.clone_run_for_approval(
        source_run_id,
        RequirementRequest(
            source_url="https://tenant.feishu.cn/docx/clone-approved-source"
        ),
        run_id=clone_run_id,
        thread_id=clone_thread_id,
    )
    cloned = await runtime.get_run_view(clone_run_id)
    snapshot = await graph.aget_state(
        {"configurable": {"thread_id": clone_thread_id}}
    )
    clone_state = snapshot.values

    assert cloned["status"] == "waiting_approval"
    assert {
        "tasks": cloned["approval"]["tasks"],
        "document_summary": cloned["approval"]["document_summary"],
        "excluded_assets": cloned["approval"]["excluded_assets"],
    } == approved
    assert clone_state["draft_plan"] == approved
    assert clone_state["task_plan"] == approved
    assert clone_state["approved_tasks"] == approved["tasks"]
    assert clone_state["approved_plan"] is None
    assert clone_state["approval_decision"] is None
    assert clone_state["approval_revision"] is None
    assert clone_state["execution_records"] == []
    assert clone_state["artifacts"] == []
    assert clone_state["delivery_record"] is None
    assert services.image_generator.submit_calls == image_submit_calls
    assert services.video_generator.submit_calls == submit_calls
    assert services.delivery_writer.deliver_calls == delivery_calls
    assert services.planner.plan_calls == planner_calls


async def test_clone_uses_complete_approved_plan_without_generation(
    fake_services: GraphServices,
) -> None:
    graph = build_graph(fake_services, InMemorySaver())
    runtime = GraphRuntime(
        graph=graph,
        repository=fake_services.repository,
        file_store=fake_services.file_store,
        settings=fake_services.settings,
        delivery_writer=fake_services.delivery_writer,
    )
    try:
        approved = await _complete_run_with_approval_edits(
            runtime,
            graph,
            fake_services,
            run_id="clone-approved-original",
            thread_id="clone-approved-original-thread",
        )
        await _assert_cloned_approval_matches(
            runtime,
            graph,
            fake_services,
            approved,
            source_run_id="clone-approved-original",
            clone_run_id="clone-approved-new",
            clone_thread_id="clone-approved-new-thread",
        )
    finally:
        await runtime.close()


async def test_clone_rebuilds_complete_plan_from_legacy_checkpoint(
    fake_services: GraphServices,
) -> None:
    graph = build_graph(fake_services, InMemorySaver())
    runtime = GraphRuntime(
        graph=graph,
        repository=fake_services.repository,
        file_store=fake_services.file_store,
        settings=fake_services.settings,
        delivery_writer=fake_services.delivery_writer,
    )
    try:
        approved = await _complete_run_with_approval_edits(
            runtime,
            graph,
            fake_services,
            run_id="clone-legacy-original",
            thread_id="clone-legacy-original-thread",
        )
        await graph.aupdate_state(
            {"configurable": {"thread_id": "clone-legacy-original-thread"}},
            {"approved_plan": None},
            as_node="deliver_to_feishu",
        )
        await _assert_cloned_approval_matches(
            runtime,
            graph,
            fake_services,
            approved,
            source_run_id="clone-legacy-original",
            clone_run_id="clone-legacy-new",
            clone_thread_id="clone-legacy-new-thread",
        )
    finally:
        await runtime.close()


async def test_runtime_graph_calls_do_not_trace_full_prompt_snapshot(
    fake_services: GraphServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_prompt = "真实 LangGraph 根追踪绝不能记录的个人完整提示词"
    planning_prompt = build_planning_prompt_snapshot(
        owner_user_id="portal-user-a",
        source="personal",
        version=2,
        prompt_text=secret_prompt,
    )
    graph = build_graph(fake_services, InMemorySaver())
    runtime = GraphRuntime(
        graph=graph,
        repository=fake_services.repository,
        file_store=fake_services.file_store,
        settings=fake_services.settings,
        delivery_writer=fake_services.delivery_writer,
    )

    class _Recorder(BaseCallbackHandler):
        def __init__(self) -> None:
            self.inputs: list[Any] = []
            self.outputs: list[Any] = []

        def on_chain_start(
            self,
            serialized: dict[str, Any],
            inputs: Any,
            **kwargs: Any,
        ) -> None:
            del serialized, kwargs
            self.inputs.append(copy.deepcopy(inputs))

        def on_chain_end(self, outputs: Any, **kwargs: Any) -> None:
            del kwargs
            self.outputs.append(copy.deepcopy(outputs))

    class _NoopLangChainTracer(BaseCallbackHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def set_defaults(self, **kwargs: Any) -> None:
            del kwargs

    monkeypatch.setattr(
        "langchain_core.tracers.langchain.LangChainTracer",
        _NoopLangChainTracer,
    )
    recorder = _Recorder()
    try:
        with tracing_context(enabled=True):
            with set_config_context({"callbacks": [recorder]}) as context:
                start_task = context.run(
                    asyncio.create_task,
                    runtime.start_run(
                        RequirementRequest(
                            source_url="https://tenant.feishu.cn/docx/private",
                            planning_prompt=planning_prompt,
                        ),
                        run_id="trace-private-run",
                        thread_id="trace-private-thread",
                    ),
                )
                run_id = await start_task
                for _ in range(100):
                    run = await runtime.get_run_view(run_id)
                    if run["status"] == "waiting_approval":
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("run did not reach approval")
                snapshot = await graph.aget_state(
                    {"configurable": {"thread_id": "trace-private-thread"}}
                )
    finally:
        await runtime.close()

    recorded = repr(recorder.inputs) + repr(recorder.outputs)
    assert recorder.inputs == []
    assert recorder.outputs == []
    assert secret_prompt not in recorded
    assert snapshot.values["planning_prompt"]["prompt_text"] == secret_prompt
    assert fake_services.image_generator.submit_calls == 0
    assert fake_services.video_generator.submit_calls == 0


def _png_bytes(color: tuple[int, int, int] = (40, 110, 210)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (24, 18), color).save(output, format="PNG")
    return output.getvalue()


def _source_asset(path: Path, asset_id: str) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "source_block_id": f"image-{asset_id}",
        "origin": "feishu",
        "file_token": None,
        "local_path": str(path),
        "mime_type": "image/png",
        "size": path.stat().st_size,
        "sha256": f"sha-{asset_id}",
        "width": 16,
        "height": 16,
        "download_error": None,
    }


def _normalized_document(
    media_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document_id": "doc-test",
        "title": "纸船需求",
        "revision": 7,
        "source_type": "docx",
        "source_token": "doc-test",
        "blocks": [
            {
                "block_id": "story-1",
                "parent_id": None,
                "block_type": "text",
                "order": 0,
                "path": [],
                "text": "生成纸船图片与视频",
            },
            {
                "block_id": "image-request",
                "parent_id": None,
                "block_type": "text",
                "order": 1,
                "path": [],
                "text": "生成纸船海报",
            },
        ],
        "text_view": "生成纸船图片与视频",
        "media_assets": media_assets,
        "ingest_issues": [],
    }


async def test_create_run_and_read_waiting_approval(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        created = await client.post(
            "/api/runs",
            json={"source_url": "https://acme.feishu.cn/docx/doccn123"},
        )

        assert created.status_code == 202
        run_id = created.json()["run_id"]
        view = await _wait_for_status(client, run_id, "waiting_approval")
        assert view["approval"]["tasks"][0]["task_id"] == "task-1"
        assert view["thread_id"] != run_id
        assert view["events"][-1]["node"] == "validate_plan"
        assert view["interrupt"] == {
            "action": "review_plan",
            "status": "waiting_approval",
        }


async def test_delete_waiting_run_removes_api_view(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        created = await client.post(
            "/api/runs",
            json={"source_url": "https://acme.feishu.cn/docx/delete-me"},
        )
        run_id = created.json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")

        removed = await client.delete(f"/api/runs/{run_id}")

        assert removed.status_code == 200
        assert removed.json()["status"] == "deleted"
        assert (await client.get(f"/api/runs/{run_id}")).status_code == 404


async def test_approval_rejects_unknown_task_id(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        created = await client.post(
            "/api/runs",
            json={"source_url": "https://acme.feishu.cn/docx/doccn123"},
        )
        run_id = created.json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")

        response = await client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "action": "approve",
                "selected_task_ids": ["missing"],
                "tasks": [],
            },
        )

        assert response.status_code == 422
        assert "missing" in response.text


async def test_create_rejects_empty_and_non_feishu_links(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        for source_url in (
            "",
            "http://acme.feishu.cn/docx/doccn123",
            "https://example.com/docx/doccn123",
        ):
            response = await client.post(
                "/api/runs",
                json={"source_url": source_url},
            )
            assert response.status_code == 422
            assert response.json()["detail"]


async def test_missing_run_returns_404(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        response = await client.get("/api/runs/missing")
        assert response.status_code == 404
        assert "不存在" in response.text


async def test_reject_cancel_and_partial_approve_routes(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, repository
        reject_run = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/reject"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, reject_run, "waiting_approval")
        rejected = await client.post(
            f"/api/runs/{reject_run}/decision",
            json={"action": "reject", "feedback": "画面改为暖色"},
        )
        assert rejected.status_code == 202
        await _wait_for_status(client, reject_run, "waiting_approval")

        cancel_run = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/cancel"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, cancel_run, "waiting_approval")
        cancelled = await client.post(
            f"/api/runs/{cancel_run}/decision",
            json={"action": "cancel"},
        )
        assert cancelled.status_code == 202
        await _wait_for_status(client, cancel_run, "cancelled")

        approve_run = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/approve"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, approve_run, "waiting_approval")
        approved = await client.post(
            f"/api/runs/{approve_run}/decision",
            json={"action": "approve", "selected_task_ids": ["task-2"]},
        )
        assert approved.status_code == 202
        view = await _wait_for_status(client, approve_run, "approved")
        assert view["approval"]["selected_task_ids"] == ["task-2"]
        assert graph.resume_calls == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "unknown"},
        {"action": "approve", "selected_task_ids": []},
        {
            "action": "approve",
            "selected_task_ids": ["task-1", "task-1"],
        },
        {"action": "reject", "feedback": ""},
        {"action": "cancel", "selected_task_ids": ["task-1"]},
    ],
)
async def test_invalid_decision_shapes_return_readable_422(
    tmp_path: Path,
    payload: dict[str, Any],
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/invalid"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        response = await client.post(
            f"/api/runs/{run_id}/decision",
            json=payload,
        )
        assert response.status_code == 422
        assert response.json()["detail"]
        assert graph.resume_calls == 0


@pytest.mark.parametrize(
    "task_update",
    [
        {"blocking_issues": ["图片用途不明确"]},
        {
            "reference_images": [
                {"asset_id": "missing", "role": "reference_image", "order": 1}
            ]
        },
        {
            "reference_images": [
                {"asset_id": "asset-1", "role": "reference_image", "order": 1},
                {"asset_id": "asset-1", "role": "reference_image", "order": 1},
            ]
        },
        {
            "reference_images": [
                {"asset_id": "asset-1", "role": "first_frame", "order": 1},
                {"asset_id": "asset-1", "role": "first_frame", "order": 2},
            ]
        },
    ],
)
async def test_invalid_edited_task_is_rejected_before_resume(
    tmp_path: Path,
    task_update: dict[str, Any],
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/edit"},
            )
        ).json()["run_id"]
        view = await _wait_for_status(client, run_id, "waiting_approval")
        edited = dict(view["approval"]["tasks"][0])
        edited.update(task_update)
        response = await client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "action": "approve",
                "selected_task_ids": ["task-1"],
                "tasks": [edited],
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"]
        assert graph.resume_calls == 0


async def test_concurrent_decision_only_resumes_once(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/double"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        graph.resume_release = asyncio.Event()
        payload = {"action": "approve", "selected_task_ids": ["task-1"]}
        first = asyncio.create_task(
            client.post(f"/api/runs/{run_id}/decision", json=payload)
        )
        await asyncio.wait_for(graph.resume_started.wait(), timeout=1)

        second = await client.post(
            f"/api/runs/{run_id}/decision",
            json=payload,
        )
        graph.resume_release.set()
        first_response = await asyncio.wait_for(first, timeout=1)

        assert sorted([first_response.status_code, second.status_code]) == [202, 409]
        assert graph.resume_calls == 1


async def test_background_failure_is_safe_and_sets_failed(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime
        graph.fail_initial = True
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/failure"},
            )
        ).json()["run_id"]
        view = await _wait_for_status(client, run_id, "failed")
        events = await repository.list_events(run_id)
        serialized = str(view) + str(events)
        assert "fictional-secret-background-failure" not in serialized
        assert events[-1]["node"] == "runtime"
        assert events[-1]["status"] == "failed"


async def test_run_view_omits_paths_tokens_keys_and_raw_document(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={
                    "source_url": (
                        "https://acme.feishu.cn/docx/safe"
                        "?token=fictional-query-secret#fragment"
                    )
                },
            )
        ).json()["run_id"]
        view = await _wait_for_status(client, run_id, "waiting_approval")
        serialized = str(view)
        assert view["source_url"] == "https://acme.feishu.cn/docx/safe"
        assert "fictional-query-secret" not in serialized
        assert str(tmp_path) not in serialized
        assert "local_path" not in serialized
        assert "file_token" not in serialized
        assert "normalized_document" not in serialized
        assert "base64" not in serialized.lower()


async def test_add_reference_uses_verified_image_and_invalidates_approval(
    tmp_path: Path,
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/add-ref"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        run = await repository.get_run(run_id)
        assert run is not None
        state = graph.states[run["thread_id"]]
        state["approval_decision"] = {"action": "approve"}
        state["approved_tasks"] = [_task()]

        response = await client.post(
            f"/api/runs/{run_id}/references",
            data={"task_id": "task-1", "role": "reference_image", "order": "2"},
            files={"file": ("not-trusted.txt", _png_bytes(), "text/plain")},
        )

        assert response.status_code == 201
        asset_id = response.json()["asset_id"]
        view = (await client.get(f"/api/runs/{run_id}")).json()
        uploaded = next(
            asset
            for asset in view["approval"]["media_assets"]
            if asset["asset_id"] == asset_id
        )
        assert uploaded["mime_type"] == "image/png"
        assert uploaded["size"] == len(_png_bytes())
        task = view["approval"]["tasks"][0]
        assert task["reference_images"][-1] == {
            "asset_id": asset_id,
            "role": "reference_image",
            "order": 2,
        }
        assert state["approval_decision"] is None
        assert state["approved_tasks"] == []
        assert state["draft_revision"] == 8
        assert view["approval"]["revision"] == 8


async def test_add_reference_accepts_verified_video_and_audio(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/add-media-ref"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        video = await client.post(
            f"/api/runs/{run_id}/references",
            data={"task_id": "task-1", "role": "reference_video", "order": "2"},
            files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypisom", "video/mp4")},
        )
        audio = await client.post(
            f"/api/runs/{run_id}/references",
            data={"task_id": "task-1", "role": "reference_audio", "order": "3"},
            files={"file": ("music.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00", "audio/mpeg")},
        )

        assert video.status_code == 201
        assert audio.status_code == 201
        audio_id = audio.json()["asset_id"]
        content = await client.get(f"/api/runs/{run_id}/references/{audio_id}/content")
        assert content.headers["content-type"].startswith("audio/mpeg")


async def test_replace_and_unlink_reference_keep_content_file(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/replace-ref"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        replaced = await client.post(
            f"/api/runs/{run_id}/references",
            data={
                "task_id": "task-1",
                "role": "reference_image",
                "order": "1",
                "replaces_asset_id": "asset-1",
            },
            files={"file": ("replacement.png", _png_bytes((20, 180, 80)), "image/png")},
        )
        assert replaced.status_code == 201
        replacement_id = replaced.json()["asset_id"]
        added = await client.post(
            f"/api/runs/{run_id}/references",
            data={
                "task_id": "task-1",
                "role": "reference_image",
                "order": "2",
            },
            files={"file": ("last.png", _png_bytes((220, 80, 40)), "image/png")},
        )
        assert added.status_code == 201
        last_id = added.json()["asset_id"]

        before = (await client.get(f"/api/runs/{run_id}")).json()
        refs = before["approval"]["tasks"][0]["reference_images"]
        assert [ref["asset_id"] for ref in refs] == [replacement_id, last_id]
        assert "asset-1" in {
            asset["asset_id"] for asset in before["approval"]["media_assets"]
        }
        content = await client.get(
            f"/api/runs/{run_id}/references/{last_id}/content"
        )
        assert content.status_code == 200
        assert content.headers["content-type"].startswith("image/png")

        removed = await client.delete(
            f"/api/runs/{run_id}/tasks/task-1/references/{last_id}"
        )
        assert removed.status_code == 200
        retained_content = await client.get(
            f"/api/runs/{run_id}/references/{last_id}/content"
        )
        assert retained_content.status_code == 200
        after = (await client.get(f"/api/runs/{run_id}")).json()
        assert after["approval"]["tasks"][0]["reference_images"] == [
            {"asset_id": replacement_id, "role": "reference_image", "order": 1}
        ]


async def test_reference_edits_reconcile_exclusions_on_the_server(
    tmp_path: Path,
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/coverage-edit"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        run = await repository.get_run(run_id)
        assert run is not None
        state = graph.states[run["thread_id"]]
        second_path = tmp_path / "data" / "second-source.png"
        second_path.write_bytes(_png_bytes((180, 80, 40)))
        second = _source_asset(second_path, "asset-2")
        state["media_assets"].append(second)
        state["draft_plan"]["excluded_assets"] = [
            {"asset_id": "asset-2", "reason": "供应商数量限制，暂不使用此图。"}
        ]
        state["task_plan"] = copy.deepcopy(state["draft_plan"])
        state["normalized_document"] = _normalized_document(state["media_assets"])

        added = await client.patch(
            f"/api/runs/{run_id}/tasks/task-1/references",
            json={
                "references": [
                    {"asset_id": "asset-1", "role": "reference_image", "order": 1},
                    {"asset_id": "asset-2", "role": "reference_image", "order": 2},
                ],
                "reference_mode": "multi_reference",
            },
        )
        assert added.status_code == 200
        view = (await client.get(f"/api/runs/{run_id}")).json()
        assert view["approval"]["excluded_assets"] == []

        state["draft_plan"]["tasks"][1]["reference_images"] = [
            {"asset_id": "asset-2", "role": "reference_image", "order": 1}
        ]
        state["task_plan"] = copy.deepcopy(state["draft_plan"])
        removed = await client.delete(
            f"/api/runs/{run_id}/tasks/task-1/references/asset-1"
        )
        assert removed.status_code == 200
        view = (await client.get(f"/api/runs/{run_id}")).json()
        assert view["approval"]["excluded_assets"] == [
            {"asset_id": "asset-1", "reason": "用户在审批中移除"}
        ]


async def test_replacing_and_uploading_reference_updates_coverage_atomically(
    tmp_path: Path,
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/coverage-replace"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        run = await repository.get_run(run_id)
        assert run is not None
        state = graph.states[run["thread_id"]]
        second_path = tmp_path / "data" / "replace-source.png"
        second_path.write_bytes(_png_bytes((80, 180, 40)))
        second = _source_asset(second_path, "asset-2")
        state["media_assets"].append(second)
        state["draft_plan"]["tasks"][1]["reference_images"] = [
            {"asset_id": "asset-2", "role": "reference_image", "order": 1}
        ]
        state["draft_plan"]["excluded_assets"] = []
        state["task_plan"] = copy.deepcopy(state["draft_plan"])
        state["normalized_document"] = _normalized_document(state["media_assets"])

        replaced = await client.post(
            f"/api/runs/{run_id}/references",
            data={
                "task_id": "task-1",
                "role": "reference_image",
                "order": "1",
                "replaces_asset_id": "asset-1",
            },
            files={"file": ("replacement.png", _png_bytes(), "image/png")},
        )

        assert replaced.status_code == 201
        replacement_id = replaced.json()["asset_id"]
        view = (await client.get(f"/api/runs/{run_id}")).json()
        assert view["approval"]["tasks"][0]["reference_images"][0]["asset_id"] == (
            replacement_id
        )
        assert view["approval"]["excluded_assets"] == [
            {"asset_id": "asset-1", "reason": "用户在审批中移除"}
        ]
        assert view["approval"]["coverage"] == {
            "successful_total": 3,
            "referenced_count": 2,
            "excluded_count": 1,
            "uncovered_count": 0,
            "failed_count": 0,
        }


async def test_approval_with_uncovered_asset_returns_422_before_execution(
    tmp_path: Path,
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/coverage-approve"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        run = await repository.get_run(run_id)
        assert run is not None
        state = graph.states[run["thread_id"]]
        second_path = tmp_path / "data" / "uncovered-source.png"
        second_path.write_bytes(_png_bytes((30, 160, 190)))
        state["media_assets"].append(_source_asset(second_path, "asset-2"))
        state["draft_plan"]["excluded_assets"] = []
        state["task_plan"] = copy.deepcopy(state["draft_plan"])
        state["normalized_document"] = _normalized_document(state["media_assets"])

        response = await client.post(
            f"/api/runs/{run_id}/decision",
            json={"action": "approve", "selected_task_ids": ["task-1"]},
        )

        assert response.status_code == 422
        assert "覆盖" in response.text
        assert graph.resume_calls == 0


async def test_approval_task_edit_can_use_a_previously_excluded_asset(
    tmp_path: Path,
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/coverage-decision"},
            )
        ).json()["run_id"]
        view = await _wait_for_status(client, run_id, "waiting_approval")
        run = await repository.get_run(run_id)
        assert run is not None
        state = graph.states[run["thread_id"]]
        second_path = tmp_path / "data" / "decision-source.png"
        second_path.write_bytes(_png_bytes((120, 90, 180)))
        state["media_assets"].append(_source_asset(second_path, "asset-2"))
        state["draft_plan"]["excluded_assets"] = [
            {"asset_id": "asset-2", "reason": "初次规划未选择此素材。"}
        ]
        state["task_plan"] = copy.deepcopy(state["draft_plan"])
        state["normalized_document"] = _normalized_document(state["media_assets"])
        edited = copy.deepcopy(view["approval"]["tasks"][0])
        edited["reference_images"].append(
            {"asset_id": "asset-2", "role": "reference_image", "order": 2}
        )

        response = await client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "action": "approve",
                "selected_task_ids": ["task-1"],
                "tasks": [edited],
            },
        )

        assert response.status_code == 202
        assert graph.resume_calls == 1


async def test_reference_upload_rejects_non_image_and_unknown_replacement(
    tmp_path: Path,
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/bad-ref"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        non_image = await client.post(
            f"/api/runs/{run_id}/references",
            data={"task_id": "task-1", "role": "reference_image", "order": "2"},
            files={"file": ("payload.png", b"not-an-image", "image/png")},
        )
        assert non_image.status_code == 422
        assert "图片" in non_image.text

        unknown = await client.post(
            f"/api/runs/{run_id}/references",
            data={
                "task_id": "task-1",
                "role": "reference_image",
                "order": "2",
                "replaces_asset_id": "missing",
            },
            files={"file": ("real.png", _png_bytes(), "image/png")},
        )
        assert unknown.status_code == 422
        assert "missing" in unknown.text


async def test_reference_patch_rejects_unknown_asset_duplicate_order_and_role(
    tmp_path: Path,
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/patch-ref"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        added = await client.post(
            f"/api/runs/{run_id}/references",
            data={"task_id": "task-1", "role": "reference_image", "order": "2"},
            files={"file": ("second.png", _png_bytes(), "image/png")},
        )
        asset_id = added.json()["asset_id"]
        invalid_lists = [
            [
                {"asset_id": "missing", "role": "reference_image", "order": 1}
            ],
            [
                {"asset_id": "asset-1", "role": "reference_image", "order": 1},
                {"asset_id": asset_id, "role": "reference_image", "order": 1},
            ],
            [
                {"asset_id": "asset-1", "role": "first_frame", "order": 1},
                {"asset_id": asset_id, "role": "first_frame", "order": 2},
            ],
        ]
        for references in invalid_lists:
            response = await client.patch(
                f"/api/runs/{run_id}/tasks/task-1/references",
                json={"references": references},
            )
            assert response.status_code == 422
            assert response.json()["detail"]


async def test_reference_patch_rejects_failed_asset_atomically(
    tmp_path: Path,
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/failed-ref"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        run = await repository.get_run(run_id)
        assert run is not None
        state = graph.states[run["thread_id"]]
        failed = copy.deepcopy(state["media_assets"][0])
        failed.update(
            asset_id="asset-failed",
            source_block_id="image-failed",
            download_error="fictional download failure",
        )
        state["media_assets"].append(failed)
        before_plan = copy.deepcopy(state["draft_plan"])
        before_revision = state.get("draft_revision")

        response = await client.patch(
            f"/api/runs/{run_id}/tasks/task-1/references",
            json={
                "references": [
                    {"asset_id": "asset-1", "role": "reference_image", "order": 1},
                    {
                        "asset_id": "asset-failed",
                        "role": "reference_image",
                        "order": 2,
                    },
                ],
                "reference_mode": "multi_reference",
            },
        )

        assert response.status_code == 422
        assert "下载失败" in response.text
        assert state["draft_plan"] == before_plan
        assert state.get("draft_revision") == before_revision
        assert state["approved_tasks"] == []


async def test_reference_add_persists_in_real_graph_checkpoint(
    fake_services: GraphServices,
):
    graph = build_graph(fake_services, InMemorySaver())
    runtime = GraphRuntime(
        graph=graph,
        repository=fake_services.repository,
        file_store=fake_services.file_store,
        settings=fake_services.settings,
    )
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            run_id = (
                await client.post(
                    "/api/runs",
                    json={"source_url": "https://acme.feishu.cn/docx/doccn123"},
                )
            ).json()["run_id"]
            await _wait_for_status(client, run_id, "waiting_approval")
            uploaded = await client.post(
                f"/api/runs/{run_id}/references",
                data={
                    "task_id": "task-video",
                    "role": "reference_image",
                    "order": "2",
                },
                files={"file": ("real.png", _png_bytes(), "image/png")},
            )
            assert uploaded.status_code == 201
            run = await fake_services.repository.get_run(run_id)
            assert run is not None
            snapshot = await graph.aget_state(
                {"configurable": {"thread_id": run["thread_id"]}}
            )
            asset_id = uploaded.json()["asset_id"]
            assert asset_id in {
                asset["asset_id"] for asset in snapshot.values["media_assets"]
            }
            assert snapshot.values["draft_plan"]["tasks"][0][
                "reference_images"
            ][-1]["asset_id"] == asset_id
            assert snapshot.values["approval_decision"] is None
            assert snapshot.values["approved_tasks"] == []
            assert snapshot.values["draft_revision"] == 8
            assert snapshot.values["status"] == "waiting_approval"
            assert snapshot.next == ("human_approval",)
            assert fake_services.image_generator.submit_calls == 0
            assert fake_services.video_generator.submit_calls == 0


async def test_reference_patch_updates_role_and_order(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/update-ref"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        added = await client.post(
            f"/api/runs/{run_id}/references",
            data={"task_id": "task-1", "role": "reference_image", "order": "2"},
            files={"file": ("second.png", _png_bytes(), "image/png")},
        )
        asset_id = added.json()["asset_id"]

        updated = await client.patch(
            f"/api/runs/{run_id}/tasks/task-1/references",
            json={
                "references": [
                    {"asset_id": asset_id, "role": "first_frame", "order": 1},
                    {"asset_id": "asset-1", "role": "last_frame", "order": 2},
                ],
                "reference_mode": "first_last_frame",
            },
        )
        assert updated.status_code == 200
        view = (await client.get(f"/api/runs/{run_id}")).json()
        assert view["approval"]["tasks"][0]["reference_images"] == [
            {"asset_id": asset_id, "role": "first_frame", "order": 1},
            {"asset_id": "asset-1", "role": "last_frame", "order": 2},
        ]


async def test_reference_patch_persists_multi_reference_mode(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/multi-mode"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")

        updated = await client.patch(
            f"/api/runs/{run_id}/tasks/task-1/references",
            json={
                "references": [
                    {"asset_id": "asset-1", "role": "reference_image", "order": 1}
                ],
                "reference_mode": "multi_reference",
            },
        )

        assert updated.status_code == 200
        view = (await client.get(f"/api/runs/{run_id}")).json()
        assert view["approval"]["tasks"][0]["reference_mode"] == "multi_reference"


async def test_unlink_last_reference_and_oversized_upload_are_rejected(
    tmp_path: Path,
):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/ref-limits"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        removed = await client.delete(
            f"/api/runs/{run_id}/tasks/task-1/references/asset-1"
        )
        assert removed.status_code == 422
        assert "至少一张" in removed.text

        oversized = await client.post(
            f"/api/runs/{run_id}/references",
            data={"task_id": "task-1", "role": "reference_image", "order": "2"},
            files={
                "file": (
                    "too-large.png",
                    b"x" * (1024 * 1024 + 1),
                    "image/png",
                )
            },
        )
        assert oversized.status_code == 422
        assert "大小" in oversized.text


async def test_terminal_run_cannot_be_decided_again(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        run_id = (
            await client.post(
                "/api/runs",
                json={"source_url": "https://acme.feishu.cn/docx/terminal"},
            )
        ).json()["run_id"]
        await _wait_for_status(client, run_id, "waiting_approval")
        assert (
            await client.post(
                f"/api/runs/{run_id}/decision",
                json={"action": "cancel"},
            )
        ).status_code == 202
        again = await client.post(
            f"/api/runs/{run_id}/decision",
            json={"action": "cancel"},
        )
        assert again.status_code == 409


class BlockingInitialGraph(FakeApprovalGraph):
    def __init__(self, repository: Repository, image_path: Path) -> None:
        super().__init__(repository, image_path)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ainvoke(
        self,
        value: dict[str, Any] | Command | None,
        *,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        if isinstance(value, dict):
            self.started.set()
            await self.release.wait()
        return await super().ainvoke(value, config=config)


async def test_runtime_close_cancels_and_clears_background_tasks(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
    )
    settings.ensure_paths()
    image_path = settings.data_dir / "source.png"
    image_path.write_bytes(b"source")
    repository = await Repository.open(settings.business_db_path)
    graph = BlockingInitialGraph(repository, image_path)
    runtime = GraphRuntime(
        graph=graph,
        repository=repository,
        file_store=FileStore(
            settings.data_dir,
            settings.outputs_dir,
            max_bytes=settings.max_download_bytes,
        ),
        settings=settings,
    )
    try:
        run_id = await runtime.start_run(
            RequirementRequest(
                source_url="https://acme.feishu.cn/docx/closing"
            )
        )
        await asyncio.wait_for(graph.started.wait(), timeout=1)
        await runtime.close()
        assert runtime._background_tasks == set()
        events = await repository.list_events(run_id)
        assert not any(event["node"] == "runtime" for event in events)
    finally:
        await repository.close()
async def test_static_review_workspace_is_served_and_uses_safe_dom_updates():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            page = await client.get("/")
            script = await client.get("/static/app.js")
            review_state = await client.get("/static/review-state.js")
            styles = await client.get("/static/styles.css")
            options = await client.options(
                "/api/runs",
                headers={"Origin": "https://outside.invalid"},
            )

    assert page.status_code == 200
    assert script.status_code == 200
    assert review_state.status_code == 200
    assert styles.status_code == 200
    for text in (
        "节点轨迹",
        "当前节点",
        "当前状态",
        "耗时",
        "thread ID",
        "负面约束",
        "参考图片",
        "素材覆盖",
        "排除素材",
        "退回重新规划",
        "全部取消",
        "批准所选任务",
    ):
        assert text in page.text
    assert "setInterval" in script.text
    assert "1000" in script.text
    assert "/api/bitable/active-runs" in script.text
    assert "textContent" in script.text
    assert "response.ok" in script.text
    assert "detail" in script.text
    assert ".disabled" in script.text
    assert "coverageLabel" in script.text
    assert "excludedAssetRows" in script.text
    assert "coverage-summary" in styles.text
    assert "review-state.js" in page.text
    for source in (script.text, review_state.text):
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert unsafe not in source
    assert "grid-template-columns" in styles.text
    assert "access-control-allow-origin" not in options.headers


async def test_health_reports_capabilities_without_secrets(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
    )
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["capabilities"]["feishu_read"]["configured"] is False
    assert "secret" not in response.text.lower()
    assert "api_key" not in response.text.lower()


def test_smoke_requires_both_paid_confirmations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    constructed = 0

    def forbidden_factory(*args, **kwargs):
        del args, kwargs
        nonlocal constructed
        constructed += 1
        raise AssertionError("paid services must not be constructed")

    monkeypatch.setattr(smoke, "build_paid_smoke_runner", forbidden_factory)
    monkeypatch.delenv("ALLOW_PAID_SMOKE", raising=False)

    first = smoke.main(["https://acme.feishu.cn/docx/doccn123"])
    second = smoke.main(
        ["--confirm-paid-smoke", "https://acme.feishu.cn/docx/doccn123"]
    )

    assert first != 0
    assert second != 0
    assert "--confirm-paid-smoke" in capsys.readouterr().err
    assert constructed == 0


def test_smoke_constructs_runner_only_after_both_confirmations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Runner:
        async def run(self) -> None:
            calls.append("run")

    def factory(settings: Settings, source_url: str):
        assert isinstance(settings, Settings)
        calls.append(source_url)
        return Runner()

    monkeypatch.setattr(smoke, "build_paid_smoke_runner", factory)
    monkeypatch.setenv("ALLOW_PAID_SMOKE", "YES")

    result = smoke.main(
        ["--confirm-paid-smoke", "https://acme.feishu.cn/docx/doccn123"]
    )

    assert result == 0
    assert calls == ["https://acme.feishu.cn/docx/doccn123", "run"]


def test_review_draft_state_machine_in_node():
    project_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            "--test",
            str(project_root / "tests" / "frontend" / "review_state.test.cjs"),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_validation_error_does_not_echo_unknown_secret_field(tmp_path: Path):
    async with _environment(tmp_path) as (client, runtime, graph, repository):
        del runtime, graph, repository
        response = await client.post(
            "/api/runs",
            json={
                "source_url": "https://acme.feishu.cn/docx/safe-validation",
                "api_key": "fictional-secret-must-not-echo",
            },
        )
        assert response.status_code == 422
        assert "fictional-secret-must-not-echo" not in response.text


def test_main_binds_only_loopback_and_uses_configured_port(
    monkeypatch: pytest.MonkeyPatch,
):
    from feishu_generation_agent import main as main_module

    calls: list[dict[str, Any]] = []

    def fake_run(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["agent", "--port", "9876"])
    main_module.main()

    assert calls[0]["kwargs"]["factory"] is True
    assert calls[0]["kwargs"]["host"] == "127.0.0.1"
    assert calls[0]["kwargs"]["port"] == 9876


async def test_app_lifespan_owns_graph_checkpointer_and_runtime(
    fake_services: GraphServices,
):
    app = create_app(services=fake_services)
    transport = httpx.ASGITransport(app=app)
    active_runtime: GraphRuntime | None = None

    async with app.router.lifespan_context(app):
        active_runtime = app.state.runtime
        assert isinstance(active_runtime, GraphRuntime)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            run_id = (
                await client.post(
                    "/api/runs",
                    json={"source_url": "https://acme.feishu.cn/docx/doccn123"},
                )
            ).json()["run_id"]
            await _wait_for_status(client, run_id, "waiting_approval")

    assert active_runtime is not None
    assert active_runtime._closed is True
    assert fake_services.settings.checkpoint_db_path.is_file()
    assert fake_services.image_generator.submit_calls == 0
    assert fake_services.video_generator.submit_calls == 0
