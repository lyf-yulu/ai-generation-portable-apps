from pathlib import Path

import httpx

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.graph.runtime import RunValidationError
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
