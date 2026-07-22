import httpx
import pytest

import feishu_generation_agent.cli.config_probe as config_probe_module
from feishu_generation_agent.cli.config_probe import _http_probe, main, probe
from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain import BitableTaskSummary
from feishu_generation_agent.integrations.feishu_bitable import BitableSchema
from feishu_generation_agent.domain.production_bitable import ProductionSchema


async def test_model_probe_rejects_missing_configured_model() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "deepseek-v4-flash"}]},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        reachable, permission, message = await _http_probe(
            client,
            "GET",
            "https://api.example.invalid/models",
            headers={"Authorization": "test-value"},
            expected_model="deepseek-v4-pro",
        )

    assert reachable is True
    assert permission is False
    assert "不在模型列表" in message


async def test_offline_probe_is_actionable_and_contains_no_credentials(
    tmp_path,
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
    )

    result = await probe(settings, network=False)

    assert result["ready"] is False
    assert result["capabilities"]["local_storage"]["permission_ok"] is True
    assert result["capabilities"]["deepseek"]["message"] == "缺少配置"
    rendered = str(result).lower()
    assert "bearer " not in rendered
    assert "secret" not in rendered


def _bitable_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
        lark_app_id="cli_test",
        lark_app_secret="fictional-lark-secret",
        lark_bitable_url=(
            "https://tenant.feishu.cn/wiki/wikiTABLE"
            "?table=tblTABLE&view=vewTASKS"
        ),
        lark_bitable_table_id="tblTABLE",
        lark_bitable_view_id="vewTASKS",
        deepseek_api_key="fictional-deepseek",
        deepseek_model="deepseek-model",
        claude_api_key="fictional-claude",
        claude_model="claude-model",
        chiyun_api_key="fictional-chiyun",
        chiyun_model="chiyun-model",
        ark_api_key="fictional-ark",
        seedance_model="seedance-model",
    )


async def test_offline_probe_reports_bitable_auth_schema_and_readiness(
    tmp_path,
) -> None:
    result = await probe(_bitable_settings(tmp_path), network=False)

    assert result["ready"] is True
    assert result["capabilities"]["feishu_auth"]["configured"] is True
    assert result["capabilities"]["bitable_schema"] == {
        "configured": True,
        "reachable": None,
        "permission_ok": None,
        "message": "已配置，跳过网络检查",
    }
    assert result["capabilities"]["bitable_read"] == {
        "configured": True,
        "reachable": None,
        "permission_ok": None,
        "message": "已配置，跳过网络检查",
    }


async def test_network_probe_performs_read_only_bitable_schema_and_scan(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeFeishuClient:
        def __init__(self, settings):
            assert settings.lark_app_id == "cli_test"

        async def tenant_token(self):
            calls.append("auth")
            return "fictional-tenant-token"

        async def close(self):
            calls.append("close")

    class FakeBitableClient:
        def __init__(self, client):
            assert isinstance(client, FakeFeishuClient)

        async def resolve_location(self, location):
            calls.append("resolve")
            return location.model_copy(update={"app_token": "appTABLE"})

        async def ensure_schema(self, location):
            calls.append("schema")
            assert location.app_token == "appTABLE"
            return BitableSchema(
                title_field_id="fld-title",
                source_field_id="fld-source",
                executor_field_id="fld-executor",
                result_field_id="fld-result",
            )

        async def list_tasks(self, location, schema):
            calls.append("read")
            return [
                BitableTaskSummary(
                    record_id="rec-1",
                    display_text="任务",
                    source_url="https://tenant.feishu.cn/docx/doc1",
                )
            ]

    async def fake_http_probe(*args, **kwargs):
        del args, kwargs
        return True, True, "只读鉴权检查通过"

    monkeypatch.setattr(config_probe_module, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(
        config_probe_module, "FeishuBitableClient", FakeBitableClient
    )
    monkeypatch.setattr(config_probe_module, "_http_probe", fake_http_probe)

    result = await probe(_bitable_settings(tmp_path), network=True)

    assert result["ready"] is True
    assert result["capabilities"]["bitable_schema"]["permission_ok"] is True
    assert result["capabilities"]["bitable_read"]["permission_ok"] is True
    assert calls == ["auth", "resolve", "schema", "read", "close"]


async def test_production_probe_reads_only_and_marks_result_writes_unverified(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeFeishuClient:
        def __init__(self, settings): pass
        async def tenant_token(self):
            calls.append("GET auth")
            return "token"
        async def close(self): calls.append("close")

    class FakeProductionClient:
        def __init__(self, client): pass
        async def resolve_location(self, location):
            calls.append("GET resolve")
            return location.model_copy(update={"app_token": "appProd"})
        async def ensure_schema(self, location):
            calls.append("GET schema")
            return ProductionSchema(
                requirement_name_field_id="name", requirement_attachment_field_id="attachment",
                project_name_field_id="project", requester_field_id="requester",
                maker_field_id="maker", progress_field_id="progress",
            )
        async def list_tasks(self, location, schema, *, include_completed):
            calls.append("GET records")
            assert include_completed is False
            return []

    settings = _bitable_settings(tmp_path).model_copy(
        update={
            "lark_bitable_url": None,
            "lark_bitable_table_id": None,
            "lark_bitable_view_id": None,
            "lark_production_bitable_url": "https://tenant.feishu.cn/wiki/wikiProd?table=tblProd&view=vewProd",
            "lark_production_table_id": "tblProd",
            "lark_production_view_id": "vewProd",
            "lark_result_folder_token": "fldResults",
        }
    )
    async def fake_http_probe(*args, **kwargs): return True, True, "只读鉴权检查通过"
    monkeypatch.setattr(config_probe_module, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(config_probe_module, "ProductionBitableClient", FakeProductionClient)
    monkeypatch.setattr(config_probe_module, "_http_probe", fake_http_probe)

    result = await probe(settings, network=True)

    assert result["capabilities"]["production_bitable_read"]["permission_ok"] is True
    assert result["capabilities"]["result_bitable_write"]["permission_ok"] is None
    assert "未验证" in result["capabilities"]["result_bitable_write"]["message"]
    assert calls == ["GET auth", "GET resolve", "GET schema", "GET records", "close"]


async def test_seedance_probe_reads_model_list_instead_of_unsupported_detail(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    async def fake_http_probe(client, method, url, *, headers, expected_model):
        del client, method, headers
        calls.append((url, expected_model))
        return True, True, "只读鉴权检查通过"

    monkeypatch.setattr(config_probe_module, "_http_probe", fake_http_probe)

    await probe(_bitable_settings(tmp_path), network=True)

    seedance_calls = [
        item for item in calls if item[0].startswith("https://ark.cn-beijing")
    ]
    assert seedance_calls == [
        (
            "https://ark.cn-beijing.volces.com/api/v3/models",
            "seedance-model",
        )
    ]


def test_config_probe_accepts_explicit_network_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    async def fake_probe(settings, *, network: bool):
        del settings
        calls.append(network)
        return {"ready": False, "capabilities": {}}

    monkeypatch.setattr(config_probe_module, "probe", fake_probe)

    result = main(["--network"])

    assert result == 1
    assert calls == [True]
