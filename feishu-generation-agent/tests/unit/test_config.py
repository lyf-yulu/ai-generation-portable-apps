from pathlib import Path

import pytest
from pydantic import SecretStr

from feishu_generation_agent.config import Settings
from feishu_generation_agent.bootstrap import capability_is_configured


def test_settings_are_local_and_create_runtime_paths(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", outputs_dir=tmp_path / "outputs")
    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 8765
    settings.ensure_paths()
    assert settings.data_dir.is_dir()
    assert settings.outputs_dir.is_dir()


def test_require_reports_missing_secret_names():
    settings = Settings(deepseek_api_key=None, ark_api_key=None)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY, ARK_API_KEY"):
        settings.require("deepseek_api_key", "ark_api_key")


def test_require_reports_empty_secret_as_missing():
    settings = Settings(deepseek_api_key=SecretStr(""))
    with pytest.raises(ValueError, match="^DEEPSEEK_API_KEY$"):
        settings.require("deepseek_api_key")


def test_require_reports_whitespace_only_secret_as_missing():
    settings = Settings(ark_api_key=SecretStr(" \t"))
    with pytest.raises(ValueError, match="^ARK_API_KEY$"):
        settings.require("ark_api_key")


def test_table_mode_does_not_require_legacy_delivery_fields(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        lark_app_id="cli_test",
        lark_app_secret="secret",
        lark_bitable_url="https://example.feishu.cn/wiki/wiki123?table=tbl123&view=vew123",
        lark_bitable_table_id="tbl123",
        lark_bitable_view_id="vew123",
        lark_local_operator_open_id="ou_local",
        deepseek_api_key="deepseek",
        deepseek_model="account-visible-model",
        claude_api_key="claude",
        claude_model="claude-model",
        chiyun_api_key="chiyun",
        chiyun_model="chiyun-model",
        ark_api_key="ark",
    )
    assert capability_is_configured(settings, "bitable")
    assert capability_is_configured(settings, "generation")
    assert not capability_is_configured(settings, "legacy_delivery")


def test_local_claim_requires_operator_open_id():
    settings = Settings(lark_local_operator_open_id=None)
    assert not capability_is_configured(settings, "local_claim")
