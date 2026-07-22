from pathlib import Path

import httpx

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.bitable import TableTaskStatus
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.graph.runtime import RunValidationError
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
    def __init__(self) -> None:
        self.rerun_calls: list[str] = []
        self.rerun_error: Exception | None = None

    async def scan(self):
        return [
            ProductionTaskSummary(
                record_id="rec-no-maker",
                display_text="需求 A",
                source_url="https://tenant.feishu.cn/docx/docA",
                progress="未开始",
                snapshot=ProductionSourceSnapshot(
                    requirement_name="需求 A",
                    requirement_attachment="https://tenant.feishu.cn/docx/docA",
                ),
            )
        ]

    async def claim(self, record_id: str) -> str:
        assert record_id == "rec-no-maker"
        return "run-no-maker"

    async def validate_approval(self, run_id: str) -> None:
        assert run_id == "run-no-maker"
        raise RunValidationError("缺少需求制作人；请先在生产表补齐后再批准")

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


async def test_scan_and_missing_maker_approval_gate(tmp_path) -> None:
    runtime = _Runtime(tmp_path)
    app = create_app(runtime=runtime, bitable_service=_ProductionService())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        scanned = await client.get("/api/bitable/tasks")
        claimed = await client.post("/api/bitable/tasks/rec-no-maker/claim")
        rejected = await client.post(
            f"/api/runs/{claimed.json()['run_id']}/decision",
            json={"action": "approve", "selected_task_ids": ["task-1"]},
        )

    assert scanned.status_code == 200
    assert scanned.json()[0]["progress"] == "未开始"
    assert scanned.json()[0]["deliverable"] is False
    assert "snapshot" not in scanned.json()[0]
    assert "maker_open_id" not in scanned.json()[0]
    assert rejected.status_code == 422
    assert "缺少需求制作人" in rejected.json()["detail"]
    assert runtime.resume_calls == []


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
