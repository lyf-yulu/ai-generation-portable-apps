import httpx
import pytest

from feishu_generation_agent.bootstrap import build_image_providers
from feishu_generation_agent.config import Settings


class _StubDownloader:
    async def download(self, url: str, *, expected_mime_type: str) -> bytes:
        del url, expected_mime_type
        return b"stub"


DOWNLOADER = _StubDownloader()


def _settings(**updates: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "chiyun_api_key": "fictional-chiyun-key",
        "chiyun_base_url": "https://chiyun.work",
        "chiyun_model": "banana2-ssvip",
    }
    base.update(updates)
    return Settings(**base)


@pytest.fixture
def http_client():
    client = httpx.AsyncClient()
    yield client


def test_builds_banana_and_gpt_image2_from_chiyun(http_client, tmp_path):
    providers = build_image_providers(
        _settings(),
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    assert set(providers) == {"banana", "gpt-image2"}


def test_banana_and_gpt_image2_use_distinct_models(http_client, tmp_path):
    providers = build_image_providers(
        _settings(),
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    assert providers["banana"]._model == "banana2-ssvip"
    assert providers["gpt-image2"]._model == "gpt-image-2"


def test_custom_models_are_honoured(http_client, tmp_path):
    providers = build_image_providers(
        _settings(banana_model="nano-banana2[2K]-base", gpt_image_model="gpt-image-3"),
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    assert providers["banana"]._model == "nano-banana2[2K]-base"
    assert providers["gpt-image2"]._model == "gpt-image-3"


def test_missing_chiyun_key_yields_empty_registry(http_client, tmp_path):
    providers = build_image_providers(
        _settings(chiyun_api_key=None),
        http_client,
        staging_dir=tmp_path,
        result_downloader=DOWNLOADER,
        max_result_bytes=1024,
    )

    assert providers == {}
