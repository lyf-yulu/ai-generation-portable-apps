import logging
from pathlib import Path
from types import SimpleNamespace

import httpx

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.bitable import TableTaskStatus
from feishu_generation_agent.domain.document import PlanningPromptSnapshot
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.graph.runtime import (
    RunConflict,
    RunNotFound,
    RunValidationError,
)
from feishu_generation_agent.storage.production_tasks import ProductionTaskAlreadyClaimed
from feishu_generation_agent.web.app import create_app


class _Runtime:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            outputs_dir=tmp_path / "outputs",
            business_db_path=tmp_path / "business.sqlite3",
            checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
        )
        self.resume_calls: list[str] = []
        self.start_requests = []

    async def close(self) -> None:
        pass

    async def resume_run(self, run_id, decision) -> None:
        del decision
        self.resume_calls.append(run_id)

    async def start_run(self, request) -> str:
        self.start_requests.append(request)
        return f"run-{len(self.start_requests)}"


class _ProductionService:
    def __init__(self, *, task_type: str = "动画类") -> None:
        self.rerun_calls: list[str] = []
        self.rerun_error: Exception | None = None
        self.scan_error: Exception | None = None
        self.task_type = task_type
        self.scan_categories: list[str] = []
        self.claim_categories: list[tuple[str, str]] = []
        self.owner_calls: list[tuple[str, str]] = []
        self.planning_prompts: list[PlanningPromptSnapshot] = []
        self.run_owners = {
            "run-no-maker": "prime-local",
            "run-old": "prime-local",
        }
        self.tasks_by_category = {
            "animation": [
                ProductionTaskSummary(
                    record_id="rec-no-maker",
                    display_text="需求 A",
                    source_url="https://tenant.feishu.cn/docx/docA",
                    progress="未开始",
                    task_type=self.task_type,
                    snapshot=ProductionSourceSnapshot(
                        requirement_name="需求 A",
                        task_type=self.task_type,
                        requirement_attachment="https://tenant.feishu.cn/docx/docA",
                    ),
                )
            ],
            "portrait": [
                ProductionTaskSummary(
                    record_id="rec-portrait",
                    display_text="真人需求 A",
                    source_url="https://tenant.feishu.cn/docx/docPortrait",
                    progress="未开始",
                    task_type="真人类",
                    snapshot=ProductionSourceSnapshot(
                        requirement_name="真人需求 A",
                        task_type="真人类",
                        requirement_attachment="https://tenant.feishu.cn/docx/docPortrait",
                    ),
                )
            ],
        }

    async def scan(self, category: str = "animation"):
        self.scan_categories.append(category)
        if self.scan_error is not None:
            raise self.scan_error
        return self.tasks_by_category[category]

    async def claim(
        self,
        record_id: str,
        category: str = "animation",
        *,
        owner_user_id: str = "prime-local",
        planning_prompt: PlanningPromptSnapshot | None = None,
    ) -> str:
        self.claim_categories.append((record_id, category))
        self.owner_calls.append(("claim", owner_user_id))
        assert planning_prompt is not None
        self.planning_prompts.append(planning_prompt)
        self.run_owners["run-no-maker"] = owner_user_id
        if category == "portrait":
            assert record_id == "rec-portrait"
            return "run-no-maker"
        assert record_id == "rec-no-maker"
        if self.task_type != "动画类":
            raise RunConflict(f"{self.task_type}任务暂未启用")
        return "run-no-maker"

    async def validate_approval(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        self._require_owner(run_id, owner_user_id)
        assert run_id == "run-no-maker"
        if self.task_type != "动画类":
            raise RunValidationError(f"{self.task_type}任务暂未启用")

    async def active_runs(self, *, owner_user_id: str = "prime-local"):
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                run_id=run_id,
                display_text=run_id,
                status=TableTaskStatus.PROCESSING,
            )
            for run_id, owner in self.run_owners.items()
            if owner == owner_user_id and run_id != "run-old"
        ]

    async def recent_runs(self, *, owner_user_id: str = "prime-local"):
        from types import SimpleNamespace

        items = [
            SimpleNamespace(
                run_id="run-old", display_text="需求 A", status=TableTaskStatus.COMPLETED,
                updated_at="2026-07-22T12:00:00+00:00",
            )
        ]
        return items if self.run_owners["run-old"] == owner_user_id else []

    async def rerun(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> str:
        self._require_owner(run_id, owner_user_id)
        self.rerun_calls.append(run_id)
        if self.rerun_error is not None:
            raise self.rerun_error
        return "run-new"

    async def result_table_url(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> str | None:
        self._require_owner(run_id, owner_user_id)
        assert run_id == "run-old"
        return "https://tenant.feishu.cn/base/result-table"

    async def retry_delivery(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        self._require_owner(run_id, owner_user_id)

    async def delete_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        self._require_owner(run_id, owner_user_id)

    async def sync_once(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        self._require_owner(run_id, owner_user_id)

    def _require_owner(self, run_id: str, owner_user_id: str) -> None:
        if self.run_owners.get(run_id) != owner_user_id:
            raise RunNotFound("多维表格运行不存在")

    async def close(self) -> None:
        pass


class _PromptStore:
    def __init__(self, prompt_text: str, version: int) -> None:
        self.profile = SimpleNamespace(
            prompt_text=prompt_text,
            version=version,
        )
        self.get_calls: list[str] = []

    async def get(self, portal_user_id: str):
        self.get_calls.append(portal_user_id)
        return self.profile


async def test_claim_snapshots_profile_once_and_new_claim_uses_new_version(
    tmp_path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    prompt_store = _PromptStore("个人版本 v2", 2)
    app = create_app(
        runtime=runtime,
        bitable_service=production,
        planner_prompt_store=prompt_store,
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"X-Portal-User-Id": "user-a"}

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        first = await client.post(
            "/api/bitable/tasks/rec-no-maker/claim", headers=headers
        )
        prompt_store.profile = SimpleNamespace(
            prompt_text="个人版本 v3",
            version=3,
        )
        second = await client.post(
            "/api/bitable/tasks/rec-no-maker/claim", headers=headers
        )
        production.run_owners["run-old"] = "user-a"
        before_rerun = list(prompt_store.get_calls)
        rerun = await client.post(
            "/api/bitable/runs/run-old/rerun", headers=headers
        )

    assert first.status_code == second.status_code == 202
    assert rerun.status_code == 202
    assert [item.prompt_text for item in production.planning_prompts] == [
        "个人版本 v2",
        "个人版本 v3",
    ]
    assert [item.version for item in production.planning_prompts] == [2, 3]
    assert all(
        item.owner_user_id == "user-a"
        and item.source == "personal"
        for item in production.planning_prompts
    )
    assert prompt_store.get_calls == before_rerun == ["user-a", "user-a"]
    assert "个人版本 v2" not in caplog.text
    assert "个人版本 v3" not in caplog.text
    assert "owner_user_id=user-a" in caplog.text
    assert "source=personal" in caplog.text
    assert "version=2" in caplog.text
    assert production.planning_prompts[0].prompt_sha256 in caplog.text


async def test_local_and_portal_direct_run_creation_have_explicit_snapshots(
    tmp_path,
) -> None:
    runtime = _Runtime(tmp_path)
    prompt_store = _PromptStore("个人规划提示词", 4)
    app = create_app(runtime=runtime, planner_prompt_store=prompt_store)
    transport = httpx.ASGITransport(app=app)
    payload = {"source_url": "https://tenant.feishu.cn/docx/docA"}

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        local = await client.post("/api/runs", json=payload)
        portal = await client.post(
            "/api/runs",
            json=payload,
            headers={"X-Portal-User-Id": "user-a"},
        )

    assert local.status_code == portal.status_code == 202
    local_prompt = runtime.start_requests[0].planning_prompt
    portal_prompt = runtime.start_requests[1].planning_prompt
    assert local_prompt is not None
    assert local_prompt.owner_user_id == "prime-local"
    assert local_prompt.source == "prime"
    assert local_prompt.version == 0
    assert local_prompt.prompt_sha256 == (
        "fc009b4bb8351502a9412b88a5554a8567a9aa9a633eba588fb673b513f16db1"
    )
    assert portal_prompt is not None
    assert portal_prompt.owner_user_id == "user-a"
    assert portal_prompt.source == "personal"
    assert portal_prompt.version == 4
    assert portal_prompt.prompt_text == "个人规划提示词"


async def test_portal_creation_fails_when_prompt_store_is_unavailable(
    tmp_path,
) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/runs",
            json={"source_url": "https://tenant.feishu.cn/docx/docA"},
            headers={"X-Portal-User-Id": "user-a"},
        )
        claim = await client.post(
            "/api/bitable/tasks/rec-no-maker/claim",
            headers={"X-Portal-User-Id": "user-a"},
        )

    assert response.status_code == 503
    assert claim.status_code == 503
    assert runtime.start_requests == []
    assert production.planning_prompts == []


async def test_scan_exposes_animation_type_and_allows_approval_without_maker(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        scanned = await client.get("/api/bitable/tasks")
        claimed = await client.post("/api/bitable/tasks/rec-no-maker/claim")
        approved = await client.post(
            f"/api/runs/{claimed.json()['run_id']}/decision",
            json={"action": "approve", "selected_task_ids": ["task-1"]},
        )

    assert scanned.status_code == 200
    assert scanned.json()[0]["progress"] == "未开始"
    assert scanned.json()[0]["task_type"] == "动画类"
    assert scanned.json()[0]["deliverable"] is True
    assert "snapshot" not in scanned.json()[0]
    assert "maker_open_id" not in scanned.json()[0]
    assert approved.status_code == 202
    assert runtime.resume_calls == ["run-no-maker"]


async def test_scan_marks_live_action_as_unavailable_and_rejects_claim(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    app = create_app(
        runtime=runtime,
        bitable_service=_ProductionService(task_type="真人类"),
    )
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        scanned = await client.get("/api/bitable/tasks")
        rejected = await client.post("/api/bitable/tasks/rec-no-maker/claim")

    assert scanned.status_code == 200
    assert scanned.json()[0]["task_type"] == "真人类"
    assert scanned.json()[0]["deliverable"] is True
    assert scanned.json()[0]["delivery_block_reason"] is None
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "真人类任务暂未启用"


async def test_api_routes_scan_and_claim_to_portrait_category(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    service = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=service)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        scanned = await client.get(
            "/api/bitable/tasks", params={"category": "portrait"}
        )
        claimed = await client.post(
            "/api/bitable/tasks/rec-portrait/claim",
            params={"category": "portrait"},
        )

    assert scanned.status_code == 200
    assert scanned.json()[0]["task_type"] == "真人类"
    assert claimed.status_code == 202
    assert service.scan_categories == ["portrait"]
    assert service.claim_categories == [("rec-portrait", "portrait")]


async def test_api_rejects_unknown_production_category(tmp_path) -> None:
    app = create_app(
        runtime=_Runtime(tmp_path),
        bitable_service=_ProductionService(),
    )
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/bitable/tasks", params={"category": "other"}
        )

    assert response.status_code == 422


async def test_api_returns_validation_error_for_missing_production_category(tmp_path) -> None:
    service = _ProductionService()
    service.scan_error = RunValidationError("未配置 portrait 类别")
    app = create_app(runtime=_Runtime(tmp_path), bitable_service=service)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/bitable/tasks", params={"category": "portrait"}
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "未配置 portrait 类别"


async def test_recent_runs_and_rerun_endpoints(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        recent = await client.get("/api/bitable/recent-runs")
        rerun = await client.post("/api/bitable/runs/run-old/rerun")

    assert recent.status_code == 200
    assert recent.json()[0]["run_id"] == "run-old"
    assert recent.json()[0]["rerunnable"] is True
    assert rerun.status_code == 202
    assert rerun.json() == {"run_id": "run-new"}
    assert production.rerun_calls == ["run-old"]


async def test_production_routes_filter_lists_and_hide_wrong_owner(
    tmp_path,
) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    production.run_owners.update(
        {"run-a": "user-a", "run-b": "user-b", "run-old": "user-a"}
    )
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)
    user_a = {"X-Portal-User-Id": "user-a"}
    user_b = {"X-Portal-User-Id": "user-b"}

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        scanned_a = await client.get(
            "/api/bitable/tasks", headers=user_a
        )
        scanned_b = await client.get(
            "/api/bitable/tasks", headers=user_b
        )
        active_a = await client.get(
            "/api/bitable/active-runs", headers=user_a
        )
        recent_a = await client.get(
            "/api/bitable/recent-runs", headers=user_a
        )
        wrong_owner = [
            await client.post(
                "/api/bitable/runs/run-old/rerun", headers=user_b
            ),
            await client.post(
                "/api/bitable/runs/run-old/retry-delivery",
                headers=user_b,
            ),
            await client.delete("/api/runs/run-old", headers=user_b),
        ]

    assert scanned_a.json() == scanned_b.json()
    assert [item["run_id"] for item in active_a.json()] == ["run-a"]
    assert [item["run_id"] for item in recent_a.json()] == ["run-old"]
    assert [response.status_code for response in wrong_owner] == [404, 404, 404]


async def test_rerun_of_locked_production_task_returns_a_conflict(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    production = _ProductionService()
    production.rerun_error = ProductionTaskAlreadyClaimed("生产表任务已被领取")
    app = create_app(runtime=runtime, bitable_service=production)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post("/api/bitable/runs/run-old/rerun")

    assert response.status_code == 409
    assert response.json()["detail"] == "该任务已被领取或当前不可处理"


async def test_static_assets_are_not_cached_between_local_updates(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/static/review-state.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
