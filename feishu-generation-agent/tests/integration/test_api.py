import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from PIL import Image

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.runtime import GraphRuntime
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.repository import Repository
from feishu_generation_agent.web.app import create_app


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
        self.states[self._thread_id(config)].update(values)


@asynccontextmanager
async def _environment(tmp_path: Path):
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
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                yield client, runtime, graph, repository
    finally:
        await repository.close()


async def _wait_for_status(
    client: httpx.AsyncClient,
    run_id: str,
    expected: str,
) -> dict[str, Any]:
    for _ in range(100):
        response = await client.get(f"/api/runs/{run_id}")
        if response.status_code == 200 and response.json()["status"] == expected:
            return response.json()
        await asyncio.sleep(0.01)
    raise AssertionError(f"run did not reach {expected}")


def _png_bytes(color: tuple[int, int, int] = (40, 110, 210)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (24, 18), color).save(output, format="PNG")
    return output.getvalue()


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
                "role": "first_frame",
                "order": "1",
                "replaces_asset_id": "asset-1",
            },
            files={"file": ("replacement.png", _png_bytes((20, 180, 80)), "image/png")},
        )
        assert replaced.status_code == 201
        replacement_id = replaced.json()["asset_id"]
        added = await client.post(
            f"/api/runs/{run_id}/references",
            data={"task_id": "task-1", "role": "last_frame", "order": "2"},
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
            {"asset_id": replacement_id, "role": "first_frame", "order": 1}
        ]


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
                ]
            },
        )
        assert updated.status_code == 200
        view = (await client.get(f"/api/runs/{run_id}")).json()
        assert view["approval"]["tasks"][0]["reference_images"] == [
            {"asset_id": asset_id, "role": "first_frame", "order": 1},
            {"asset_id": "asset-1", "role": "last_frame", "order": 2},
        ]


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
            styles = await client.get("/static/styles.css")
            options = await client.options(
                "/api/runs",
                headers={"Origin": "https://outside.invalid"},
            )

    assert page.status_code == 200
    assert script.status_code == 200
    assert styles.status_code == 200
    for text in (
        "节点轨迹",
        "当前节点",
        "当前状态",
        "耗时",
        "thread ID",
        "负面约束",
        "参考图片",
        "退回重新规划",
        "全部取消",
        "批准所选任务",
    ):
        assert text in page.text
    assert "setInterval" in script.text
    assert "1000" in script.text
    assert "textContent" in script.text
    assert "response.ok" in script.text
    assert "detail" in script.text
    assert ".disabled" in script.text
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert unsafe not in script.text
    assert "grid-template-columns" in styles.text
    assert "access-control-allow-origin" not in options.headers


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
