from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from feishu_generation_agent.config import Settings
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.integrations.chiyun import ChiyunImageGenerator
from feishu_generation_agent.integrations.feishu_client import FeishuClient
from feishu_generation_agent.integrations.feishu_delivery import (
    FeishuDeliveryWriter,
)
from feishu_generation_agent.integrations.feishu_source import (
    FeishuDocumentSource,
)
from feishu_generation_agent.integrations.planner import DeepSeekPlanner
from feishu_generation_agent.integrations.safe_download import (
    SafeResultDownloader,
)
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator
from feishu_generation_agent.integrations.vision import ClaudeVisionAnalyzer
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.provider_results import ProviderResultStore
from feishu_generation_agent.storage.repository import Repository


REQUIRED_RUNTIME_FIELDS = (
    "lark_app_id",
    "lark_app_secret",
    "lark_output_owner_open_id",
    "lark_output_folder_token",
    "deepseek_api_key",
    "claude_api_key",
    "claude_model",
    "chiyun_api_key",
    "chiyun_model",
    "ark_api_key",
)


def runtime_is_configured(settings: Settings) -> bool:
    try:
        settings.require(*REQUIRED_RUNTIME_FIELDS)
    except ValueError:
        return False
    return True


@asynccontextmanager
async def open_services(settings: Settings) -> AsyncIterator[GraphServices]:
    settings.require(*REQUIRED_RUNTIME_FIELDS)
    settings.ensure_paths()
    repository = await Repository.open(settings.business_db_path)
    provider_http = httpx.AsyncClient(trust_env=False)
    downloader = SafeResultDownloader(max_bytes=settings.max_download_bytes)
    feishu = FeishuClient(settings)
    file_store: FileStore | None = None
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
            delivery_writer=FeishuDeliveryWriter(
                feishu,
                repository,
                owner_open_id=settings.lark_output_owner_open_id or "",
            ),
            repository=repository,
            file_store=file_store,
            settings=settings,
        )
        yield services
    finally:
        if file_store is not None:
            file_store.close()
        await feishu.close()
        await downloader.aclose()
        await provider_http.aclose()
        await repository.close()
