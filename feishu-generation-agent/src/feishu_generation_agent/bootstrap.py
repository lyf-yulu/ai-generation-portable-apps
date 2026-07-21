from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from feishu_generation_agent.config import Settings
from feishu_generation_agent.bitable.mvp_service import BitableMvpService
from feishu_generation_agent.domain import BitableLocation
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.integrations.chiyun import ChiyunImageGenerator
from feishu_generation_agent.integrations.feishu_client import FeishuClient
from feishu_generation_agent.integrations.feishu_bitable import FeishuBitableClient
from feishu_generation_agent.integrations.bitable_delivery import BitableResultWriter
from feishu_generation_agent.integrations.bitable_url import parse_bitable_url
from feishu_generation_agent.integrations.feishu_delivery import (
    FeishuDeliveryWriter,
)
from feishu_generation_agent.integrations.feishu_source import (
    FeishuDocumentSource,
)
from feishu_generation_agent.integrations.planner import DeepSeekPlanner
from feishu_generation_agent.integrations.routing_delivery import RoutingDeliveryWriter
from feishu_generation_agent.integrations.safe_download import (
    SafeResultDownloader,
)
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator
from feishu_generation_agent.integrations.vision import ClaudeVisionAnalyzer
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.bitable_tasks import BitableTaskStore
from feishu_generation_agent.storage.provider_results import ProviderResultStore
from feishu_generation_agent.storage.repository import Repository


CAPABILITY_FIELDS: dict[str, tuple[str, ...]] = {
    "core": (
        "lark_app_id", "lark_app_secret", "deepseek_api_key",
        "claude_api_key", "claude_model",
    ),
    "generation": (
        "chiyun_api_key", "chiyun_model", "ark_api_key",
        "seedance_model",
    ),
    "bitable": (
        "lark_app_id", "lark_app_secret", "lark_bitable_url",
        "lark_bitable_table_id", "lark_bitable_view_id",
    ),
    "local_claim": ("lark_local_operator_open_id",),
    "legacy_delivery": (
        "lark_output_owner_open_id", "lark_output_folder_token",
    ),
}

# Compatibility for callers that still inspect the legacy document mode fields.
REQUIRED_RUNTIME_FIELDS = (
    *CAPABILITY_FIELDS["core"],
    *CAPABILITY_FIELDS["generation"],
    *CAPABILITY_FIELDS["legacy_delivery"],
)


def capability_is_configured(settings: Settings, name: str) -> bool:
    try:
        settings.require(*CAPABILITY_FIELDS[name])
    except (KeyError, ValueError):
        return False
    return True


def runtime_is_configured(settings: Settings) -> bool:
    return (
        capability_is_configured(settings, "core")
        and capability_is_configured(settings, "generation")
        and (
            capability_is_configured(settings, "bitable")
            or capability_is_configured(settings, "legacy_delivery")
        )
    )


@dataclass(slots=True)
class BitableServiceFactory:
    bitable: FeishuBitableClient
    store: BitableTaskStore
    location: BitableLocation
    _claimed: bool = False

    def create(self, runtime) -> BitableMvpService:
        if self._claimed:
            raise RuntimeError("多维表格服务已创建")
        self._claimed = True
        return BitableMvpService(
            bitable=self.bitable,
            store=self.store,
            runtime=runtime,
            location=self.location,
        )

    async def close_unclaimed(self) -> None:
        if not self._claimed:
            await self.store.close()


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    graph: GraphServices
    bitable_factory: BitableServiceFactory | None
    legacy_delivery_configured: bool


@asynccontextmanager
async def open_services(settings: Settings) -> AsyncIterator[GraphServices]:
    async with _open_application_services(
        settings, enable_bitable=True
    ) as application:
        yield application.graph


@asynccontextmanager
async def open_application_services(
    settings: Settings,
) -> AsyncIterator[ApplicationServices]:
    async with _open_application_services(
        settings, enable_bitable=True
    ) as application:
        yield application


@asynccontextmanager
async def _open_application_services(
    settings: Settings,
    *,
    enable_bitable: bool,
) -> AsyncIterator[ApplicationServices]:
    settings.require(*CAPABILITY_FIELDS["core"])
    settings.require(*CAPABILITY_FIELDS["generation"])
    bitable_configured = enable_bitable and capability_is_configured(
        settings, "bitable"
    )
    legacy_configured = capability_is_configured(settings, "legacy_delivery")
    if not bitable_configured and not legacy_configured:
        settings.require(*CAPABILITY_FIELDS["legacy_delivery"])
    settings.ensure_paths()
    repository = await Repository.open(settings.business_db_path)
    provider_http = httpx.AsyncClient(trust_env=False)
    downloader = SafeResultDownloader(max_bytes=settings.max_download_bytes)
    feishu = FeishuClient(settings)
    file_store: FileStore | None = None
    bitable_factory: BitableServiceFactory | None = None
    try:
        provider_results = ProviderResultStore(
            settings.data_dir / "provider-results",
            max_item_bytes=settings.max_download_bytes,
        )
        file_store = FileStore(
            settings.data_dir,
            settings.outputs_dir,
            max_bytes=settings.max_download_bytes,
            result_downloader=downloader,
            provider_result_store=provider_results,
        )
        planner_model = ChatOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            temperature=0,
            max_retries=2,
            timeout=120,
        )
        vision_options = {
            "api_key": settings.claude_api_key,
            "model_name": settings.claude_model,
            "max_tokens_to_sample": 2048,
            "temperature": 0,
            "max_retries": 2,
            "timeout": 120,
        }
        if settings.claude_base_url:
            vision_options["base_url"] = settings.claude_base_url
        vision_model = ChatAnthropic(**vision_options)
        legacy_writer = (
            FeishuDeliveryWriter(
                feishu,
                repository,
                owner_open_id=settings.lark_output_owner_open_id or "",
            )
            if legacy_configured
            else None
        )
        delivery_writer = legacy_writer
        if bitable_configured:
            location = parse_bitable_url(
                settings.lark_bitable_url or "",
                settings.lark_bitable_table_id or "",
                settings.lark_bitable_view_id or "",
            )
            bitable_client = FeishuBitableClient(feishu)
            bitable_store = await BitableTaskStore.open(
                settings.data_dir / "bitable.sqlite3"
            )
            bitable_factory = BitableServiceFactory(
                bitable=bitable_client,
                store=bitable_store,
                location=location,
            )
            bitable_writer = BitableResultWriter(
                bitable_client,
                repository,
                bitable_store,
            )
            delivery_writer = RoutingDeliveryWriter(
                bitable_store,
                bitable=bitable_writer,
                legacy=legacy_writer,
            )
        assert delivery_writer is not None
        services = GraphServices(
            document_source=FeishuDocumentSource(feishu, file_store),
            vision_analyzer=ClaudeVisionAnalyzer(
                vision_model,
                repository,
                prompt_version="v1",
                model_name=settings.claude_model,
            ),
            planner=DeepSeekPlanner(
                planner_model, max_output_count=settings.max_output_count
            ),
            image_generator=ChiyunImageGenerator(
                provider_http,
                base_url=settings.chiyun_base_url,
                api_key=settings.chiyun_api_key,
                model=settings.chiyun_model,
                staging_dir=settings.data_dir / "provider-results",
                result_downloader=downloader,
                max_result_bytes=settings.max_download_bytes,
            ),
            video_generator=SeedanceVideoGenerator(
                provider_http,
                base_url=settings.ark_base_url,
                api_key=settings.ark_api_key,
                model=settings.seedance_model,
            ),
            delivery_writer=delivery_writer,
            repository=repository,
            file_store=file_store,
            settings=settings,
        )
        yield ApplicationServices(
            graph=services,
            bitable_factory=bitable_factory,
            legacy_delivery_configured=legacy_configured,
        )
    finally:
        if bitable_factory is not None:
            await bitable_factory.close_unclaimed()
        if file_store is not None:
            file_store.close()
        await feishu.close()
        await downloader.aclose()
        await provider_http.aclose()
        await repository.close()
