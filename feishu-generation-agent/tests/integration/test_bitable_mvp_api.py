from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import feishu_generation_agent.cli.smoke as smoke_module
from feishu_generation_agent.cli.smoke import (
    BitableApprovalSmokeRunner,
    BitableReadOnlySmokeRunner,
    ProductionBitableReadOnlySmokeRunner,
)
import feishu_generation_agent.web.app as web_app_module
from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain import BitableTaskSummary
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.graph.runtime import RunConflict, RunNotFound
from feishu_generation_agent.graph.runtime import GraphRuntime
from feishu_generation_agent.integrations.bitable_delivery import (
    BitableResultConflict,
)
from feishu_generation_agent.integrations.feishu_bitable import BitableSchemaError
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
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def get_run_view(self, run_id: str) -> dict:
        if run_id == "missing":
            raise RunNotFound("运行不存在")
        return {
            "run_id": run_id,
            "thread_id": "thread-1",
            "source_url": "https://tenant.feishu.cn/docx/doc1",
            "status": "waiting_approval",
            "events": [],
            "privacy": {"langsmith_tracing": False},
            "approval": {"tasks": [], "validation_issues": []},
        }


class _BitableService:
    def __init__(self) -> None:
        self.tasks = [
            BitableTaskSummary(
                record_id="rec-1",
                display_text="雨中纸船",
                source_url="https://tenant.feishu.cn/docx/doc1",
                executor_open_ids=["ou_alice"],
                executor_names=["Alice"],
            )
        ]
        self.claimed: set[str] = set()
        self.synced: list[str] = []
        self.retried: list[str] = []
        self.closed = False
        self.scan_error: Exception | None = None
        self.retry_error: Exception | None = None

    async def scan(self) -> list[BitableTaskSummary]:
        if self.scan_error:
            raise self.scan_error
        return self.tasks

    async def claim(self, record_id: str) -> str:
        if record_id in self.claimed:
            raise RunConflict("already claimed rec-1")
        if record_id != "rec-1":
            raise RunConflict("missing record")
        self.claimed.add(record_id)
        return "run-bitable-1"

    async def sync_once(self, run_id: str) -> None:
        if run_id == "legacy-run":
            raise RunNotFound("多维表格运行不存在")
        self.synced.append(run_id)

    async def retry_delivery(self, run_id: str) -> None:
        if self.retry_error:
            raise self.retry_error
        self.retried.append(run_id)

    async def close(self) -> None:
        self.closed = True


class _ApprovalSmokeRuntime:
    def __init__(self) -> None:
        self.views = [
            {"status": "running"},
            {"status": "waiting_approval"},
        ]
        self.requested: list[str] = []

    async def get_run_view(self, run_id: str) -> dict:
        self.requested.append(run_id)
        return self.views.pop(0)


class _ApprovalSmokeService:
    def __init__(self) -> None:
        self.claimed: list[str] = []
        self.synced: list[str] = []

    async def scan(self) -> list[BitableTaskSummary]:
        return [
            BitableTaskSummary(
                record_id="rec-1",
                display_text="等待审批的任务",
                source_url="https://tenant.feishu.cn/docx/doc1",
            )
        ]

    async def claim(self, record_id: str) -> str:
        self.claimed.append(record_id)
        return "run-approval-gate"

    async def sync_once(self, run_id: str) -> None:
        self.synced.append(run_id)


async def _client(tmp_path: Path, service: _BitableService | None):
    runtime = _Runtime(tmp_path)
    app = create_app(runtime=runtime, bitable_service=service)
    transport = httpx.ASGITransport(app=app)
    return app, runtime, httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    )


async def test_scan_claim_duplicate_and_run_detail_sync(tmp_path: Path) -> None:
    service = _BitableService()
    app, runtime, client = await _client(tmp_path, service)
    async with app.router.lifespan_context(app), client:
        scanned = await client.get("/api/bitable/tasks")
        claimed = await client.post("/api/bitable/tasks/rec-1/claim")
        duplicate = await client.post("/api/bitable/tasks/rec-1/claim")
        detail = await client.get("/api/runs/run-bitable-1")

    assert scanned.status_code == 200
    assert scanned.json() == [
        {
            "record_id": "rec-1",
            "display_text": "雨中纸船",
            "source_url": "https://tenant.feishu.cn/docx/doc1",
            "status": "待处理",
            "executor_open_ids": ["ou_alice"],
            "executor_names": ["Alice"],
            "has_result": False,
        }
    ]
    assert claimed.status_code == 202
    assert claimed.json() == {"run_id": "run-bitable-1"}
    assert duplicate.status_code == 409
    assert "rec-1" not in duplicate.text
    assert detail.status_code == 200
    assert service.synced == ["run-bitable-1"]
    assert runtime.closed is True
    assert service.closed is True


async def test_bitable_approval_smoke_stops_at_waiting_approval() -> None:
    service = _ApprovalSmokeService()
    runtime = _ApprovalSmokeRuntime()
    runner = BitableApprovalSmokeRunner(
        bitable_service=service,
        runtime=runtime,
        record_id="rec-1",
        poll_interval_seconds=0,
    )

    run_id = await runner.run()

    assert run_id == "run-approval-gate"
    assert service.claimed == ["rec-1"]
    assert service.synced == ["run-approval-gate", "run-approval-gate"]
    assert runtime.requested == ["run-approval-gate", "run-approval-gate"]


async def test_bitable_read_only_smoke_does_not_claim_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Client:
        def __init__(self, settings: Settings) -> None:
            assert settings.lark_bitable_table_id == "tblTABLE"

        async def close(self) -> None:
            calls.append("close")

    class Bitable:
        def __init__(self, client: Client) -> None:
            assert isinstance(client, Client)

        async def resolve_location(self, location):
            calls.append("resolve")
            return location.model_copy(update={"app_token": "appTABLE"})

        async def ensure_schema(self, location):
            calls.append("schema")
            return object()

        async def list_tasks(self, location, schema):
            del location, schema
            calls.append("list")
            return [object(), object()]

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
        lark_app_id="cli-test",
        lark_app_secret="fictional-secret",
        lark_bitable_url=(
            "https://tenant.feishu.cn/wiki/wikiTABLE"
            "?table=tblTABLE&view=vewTASKS"
        ),
        lark_bitable_table_id="tblTABLE",
        lark_bitable_view_id="vewTASKS",
    )
    monkeypatch.setattr(smoke_module, "FeishuClient", Client)
    monkeypatch.setattr(smoke_module, "FeishuBitableClient", Bitable)

    count = await BitableReadOnlySmokeRunner(settings).run()

    assert count == 2
    assert calls == ["resolve", "schema", "list", "close"]


async def test_production_read_only_smoke_reads_one_document_without_claiming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Client:
        def __init__(self, settings): pass
        async def close(self): calls.append("close")

    class Bitable:
        def __init__(self, client): pass
        async def resolve_location(self, location):
            calls.append("GET resolve")
            return location.model_copy(update={"app_token": "appProd"})
        async def ensure_schema(self, location):
            calls.append("GET schema")
            return object()
        async def list_tasks(self, location, schema, *, include_completed):
            calls.append("GET records")
            return [SimpleNamespace(source_url="https://tenant.feishu.cn/docx/docA")]

    class Source:
        def __init__(self, client, file_store): pass
        async def ingest(self, request):
            calls.append("GET document")
            assert request.source_url.endswith("/docA")
            return object()

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data", outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3", checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
        lark_app_id="app", lark_app_secret="secret",
        lark_production_bitable_url="https://tenant.feishu.cn/wiki/wikiProd?table=tblProd&view=vewProd",
        lark_production_table_id="tblProd", lark_production_view_id="vewProd",
        lark_result_folder_token="fldResults",
    )
    monkeypatch.setattr(smoke_module, "FeishuClient", Client)
    monkeypatch.setattr(smoke_module, "ProductionBitableClient", Bitable)
    monkeypatch.setattr(smoke_module, "FeishuDocumentSource", Source)

    count = await ProductionBitableReadOnlySmokeRunner(settings).run()

    assert count == 1
    assert calls == ["GET resolve", "GET schema", "GET records", "GET document", "close"]


async def test_scan_maps_schema_and_read_errors_without_raw_details(
    tmp_path: Path,
) -> None:
    service = _BitableService()
    app, _, client = await _client(tmp_path, service)
    async with app.router.lifespan_context(app), client:
        service.scan_error = BitableSchemaError("secret field detail")
        schema = await client.get("/api/bitable/tasks")
        service.scan_error = RuntimeError("fictional-credential")
        read = await client.get("/api/bitable/tasks")

    assert schema.status_code == 422
    assert "字段" in schema.json()["detail"]
    assert "secret field detail" not in schema.text
    assert read.status_code == 502
    assert "fictional-credential" not in read.text


async def test_scan_reports_transient_feishu_failure_as_retryable(
    tmp_path: Path,
) -> None:
    service = _BitableService()
    service.scan_error = AgentError(
        ErrorDetail(
            category=ErrorCategory.TRANSIENT,
            message="飞书服务暂时不可用，请稍后重试",
            technical_detail="GET /records: HTTP 502, secret upstream detail",
            retryable=True,
        )
    )
    app, _, client = await _client(tmp_path, service)

    async with app.router.lifespan_context(app), client:
        response = await client.get("/api/bitable/tasks")

    assert response.status_code == 503
    assert response.json()["detail"] == "飞书服务暂时不可用，请稍后重试"
    assert "secret upstream detail" not in response.text


async def test_bitable_endpoints_report_readiness_when_not_configured(
    tmp_path: Path,
) -> None:
    app, _, client = await _client(tmp_path, None)
    async with app.router.lifespan_context(app), client:
        scanned = await client.get("/api/bitable/tasks")
        claimed = await client.post("/api/bitable/tasks/rec-1/claim")

    assert scanned.status_code == 503
    assert claimed.status_code == 503
    assert "尚未配置" in scanned.json()["detail"]


async def test_bitable_retry_delivery_maps_conflict_and_accepts_retry(
    tmp_path: Path,
) -> None:
    service = _BitableService()
    app, _, client = await _client(tmp_path, service)
    async with app.router.lifespan_context(app), client:
        accepted = await client.post(
            "/api/bitable/runs/run-bitable-1/retry-delivery"
        )
        service.retry_error = BitableResultConflict(
            "external record has secret attachment"
        )
        conflict = await client.post(
            "/api/bitable/runs/run-bitable-2/retry-delivery"
        )

    assert accepted.status_code == 202
    assert accepted.json() == {
        "run_id": "run-bitable-1",
        "status": "accepted",
    }
    assert service.retried == ["run-bitable-1"]
    assert conflict.status_code == 409
    assert "结果列" in conflict.json()["detail"]
    assert "secret attachment" not in conflict.text


async def test_auto_bitable_startup_delegates_recovery_before_runtime_resume(
    fake_services,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    original_resume = GraphRuntime.resume_pending_runs

    async def recording_resume(runtime):
        order.append("runtime.resume")
        await original_resume(runtime)

    class Service:
        async def resume_incomplete(self):
            order.append("service.resume")
            await active_runtime.resume_pending_runs()

        async def close(self):
            order.append("service.close")

    service = Service()
    active_runtime = None

    class Factory:
        def create(self, runtime):
            nonlocal active_runtime
            order.append("factory.create")
            active_runtime = runtime
            return service

    @asynccontextmanager
    async def fake_open_application_services(settings):
        del settings
        yield SimpleNamespace(
            graph=fake_services,
            bitable_factory=Factory(),
            legacy_delivery_configured=False,
        )

    monkeypatch.setattr(web_app_module, "runtime_is_configured", lambda settings: True)
    monkeypatch.setattr(
        web_app_module,
        "open_application_services",
        fake_open_application_services,
    )
    monkeypatch.setattr(GraphRuntime, "resume_pending_runs", recording_resume)

    app = create_app(settings=Settings(_env_file=None))
    async with app.router.lifespan_context(app):
        assert order == ["factory.create", "service.resume", "runtime.resume"]

    assert order[-1] == "service.close"
