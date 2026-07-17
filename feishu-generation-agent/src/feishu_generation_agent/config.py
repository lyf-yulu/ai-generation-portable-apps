from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    app_host: Literal["127.0.0.1"] = "127.0.0.1"
    app_port: int = 8765
    data_dir: Path = Path("data")
    outputs_dir: Path = Path("outputs")
    business_db_path: Path = Path("data/agent.sqlite3")
    checkpoint_db_path: Path = Path("data/checkpoints.sqlite3")

    lark_app_id: str | None = None
    lark_app_secret: SecretStr | None = None
    lark_output_owner_open_id: str | None = None
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: Literal["deepseek-v4-pro"] = "deepseek-v4-pro"
    claude_api_key: SecretStr | None = None
    claude_base_url: str | None = None
    claude_model: str | None = None
    chiyun_api_key: SecretStr | None = None
    chiyun_base_url: str = "https://chiyun.work"
    chiyun_model: str | None = None
    ark_api_key: SecretStr | None = None
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    seedance_model: str = "doubao-seedance-2-0-260128"
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "feishu-generation-agent-local"
    max_output_count: int = 4
    max_download_bytes: int = 500 * 1024 * 1024

    def ensure_paths(self) -> None:
        for path in (
            self.data_dir,
            self.outputs_dir,
            self.business_db_path.parent,
            self.checkpoint_db_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def require(self, *field_names: str) -> None:
        missing = []
        for name in field_names:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(name.upper())
        if missing:
            raise ValueError(", ".join(missing))
