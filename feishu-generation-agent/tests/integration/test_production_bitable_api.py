from pathlib import Path

import httpx

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.bitable import TableTaskStatus
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.graph.runtime import RunConflict, RunValidationError
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

    async def close(self) -> None:
        pass

    async def resume_run(self, run_id, decision) -> None:
        del decision
        self.resume_calls.append(run_id)


class _ProductionService:
    def __init__(self, *, task_type: str = "动画类") -> None:
        self.rerun_calls: list[str] = []
        self.rerun_error: Exception | None = None
        self.scan_error: Exception | None = None
        self.task_type = task_type
        self.scan_categories: list[str] = []
        self.claim_categories: list[tuple[str, str]] = []
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

    async def claim(self, record_id: str, category: str = "animation") -> str:
        self.claim_categories.append((record_id, category))
        if category == "portrait":
            assert record_id == "rec-portrait"
            return "run-no-maker"
        assert record_id == "rec-no-maker"
        if self.task_type != "动画类":
            raise RunConflict(f"{self.task_type}任务暂未启用")
        return "run-no-maker"

    async def validate_approval(self, run_id: str) -> None:
        assert run_id == "run-no-maker"
        if self.task_type != "动画类":
            raise RunValidationError(f"{self.task_type}任务暂未启用")

    async def recent_runs(self):
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                run_id="run-old", display_text="需求 A", status=TableTaskStatus.COMPLETED,
                updated_at="2026-07-22T12:00:00+00:00",
            )
        ]

    async def rerun(self, run_id: str) -> str:
        self.rerun_calls.append(run_id)
        if self.rerun_error is not None:
            raise self.rerun_error
        return "run-new"

    async def result_table_url(self, run_id: str) -> str | None:
        assert run_id == "run-old"
        return "https://tenant.feishu.cn/base/result-table"

    async def close(self) -> None:
        pass


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
