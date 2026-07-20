import httpx

from feishu_generation_agent.cli.config_probe import _http_probe, probe
from feishu_generation_agent.config import Settings


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
