from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    app_host: Literal["127.0.0.1"] = "127.0.0.1"
    app_port: int = 8765
    data_dir: Path = Path("data")
    outputs_dir: Path = Path("outputs")
    business_db_path: Path = Path("data/agent.sqlite3")
    checkpoint_db_path: Path = Path("data/checkpoints.sqlite3")
    asset_library_db_path: Path = Path("data/asset-library.sqlite3")
    asset_library_dir: Path = Path("data/asset-library")
    # 服务机 LAN IP 每周变动，禁止在代码里硬编码；部署时通过 .env 覆盖。
    asset_base_url: str = "http://127.0.0.1:8765"

    lark_app_id: str | None = None
    lark_app_secret: SecretStr | None = None
    lark_bitable_url: str | None = None
    lark_bitable_table_id: str | None = None
    lark_bitable_view_id: str | None = None
    lark_production_bitable_url: str | None = None
    lark_production_table_id: str | None = None
    lark_production_view_id: str | None = None
    lark_production_portrait_view_id: str | None = None
    lark_result_folder_token: str | None = None
    lark_include_completed_for_test: bool = False
    lark_local_operator_open_id: str | None = None
    lark_bot_enabled: bool = False
    lark_output_owner_open_id: str | None = None
    lark_output_folder_token: str | None = None
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    claude_api_key: SecretStr | None = None
    claude_base_url: str | None = None
    claude_model: str | None = None
    chiyun_api_key: SecretStr | None = None
    chiyun_base_url: str = "https://chiyun.work"
    chiyun_model: str | None = None
    # 图片模式的三个 provider。banana / gpt-image2 都走 chiyun 中转，
    # 靠 model 名前缀分流（gpt-image* → OpenAI 风格，其余 → Gemini 风格）。
    banana_model: str = "banana2-ssvip"
    gpt_image_model: str = "gpt-image-2"
    ark_api_key: SecretStr | None = None
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    seedance_model: str = "doubao-seedance-2-0-260128"
    volcengine_access_key: SecretStr | None = None
    volcengine_secret_key: SecretStr | None = None
    volcengine_project_name: str = "Seedance2.0"
    tos_bucket: str | None = None
    tos_region: str = "cn-beijing"
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "feishu-generation-agent-local"
    max_output_count: int = 4
    max_download_bytes: int = 500 * 1024 * 1024
    allow_benchmark_fake_ips: bool = False
    provider_poll_interval_seconds: float = Field(default=1.0, ge=0.0)
    provider_poll_max_attempts: int = Field(default=900, ge=1, le=10_000)
    submission_intent_lease_seconds: float = Field(default=180.0, ge=0.03)
    bot_scan_page_size: int = Field(default=10, ge=1, le=50)
    coordinator_poll_interval_seconds: float = Field(default=1.0, ge=0.05)

    @field_validator("asset_base_url")
    @classmethod
    def strip_asset_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            raise ValueError("asset_base_url 不能为空")
        return normalized

    def asset_public_url(self, storage_path: str) -> str:
        return f"{self.asset_base_url}/{storage_path.lstrip('/')}"

    @property
    def production_bitable_configured(self) -> bool:
        return all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                self.lark_production_bitable_url,
                self.lark_production_table_id,
                self.lark_production_view_id,
                self.lark_result_folder_token,
            )
        )

    def ensure_paths(self) -> None:
        for path in (
            self.data_dir,
            self.outputs_dir,
            self.business_db_path.parent,
            self.checkpoint_db_path.parent,
            self.asset_library_db_path.parent,
            self.asset_library_dir,
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
