from pathlib import Path

import pytest

from feishu_generation_agent.config import Settings


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
