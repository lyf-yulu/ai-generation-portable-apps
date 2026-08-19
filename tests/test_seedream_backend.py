"""Seedream 5.0 Pro provider contract and synchronous request path."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_nano():
    name = "nano_seedream_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "nano-banana" / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _values(tmp_path: Path, **overrides):
    values = {
        "api_key": "ark-key",
        "provider": "volcengine",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "mode": "text2img",
        "model": "doubao-seedream-5-0-pro-260628",
        "custom_model": "",
        "prompt": "一只纸雕小鸟",
        "aspect_ratio": "16:9",
        "image_size": "1.5K",
        "response_format": "b64_json",
        "seed": "123",
        "vary_seed": "on",
        "repeat_count": "1",
        "concurrency": "1",
        "output_dir": str(tmp_path),
        "output_name": "seedream-test",
    }
    values.update(overrides)
    return values


def test_provider_config_matches_official_seedream_5_pro_limits():
    config = json.loads((ROOT / "nano-banana" / "providers.json").read_text("utf-8"))
    provider = config["providers"]["volcengine"]

    assert provider["api_style"] == "ark_seedream"
    assert provider["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert provider["image_size_options"] == ["1K", "1.5K", "2K"]
    assert provider["max_reference_images"] == 10
    assert provider["supports_seed"] is False
    assert provider["models"] == [
        {"id": "doubao-seedream-5-0-pro-260628", "label": "Seedream 5.0 Pro"}
    ]


@pytest.mark.parametrize(
    ("resolution", "ratio", "expected"),
    [
        ("1K", "auto", "1K"),
        ("1K", "16:9", "1424x800"),
        ("1.5K", "1:1", "1536x1536"),
        ("1.5K", "9:21", "1008x2352"),
        ("2K", "3:2", "2496x1664"),
        ("2K", "21:9", "3136x1344"),
    ],
)
def test_seedream_size_uses_official_pixel_mapping(resolution, ratio, expected):
    module = _load_nano()
    assert module.seedream_size(resolution, ratio) == expected


def test_seedream_size_rejects_unsupported_4k():
    module = _load_nano()
    with pytest.raises(ValueError, match="1K、1.5K、2K"):
        module.seedream_size("4K", "1:1")


def test_resolve_api_key_uses_server_managed_ark_key():
    module = _load_nano()
    with mock.patch.dict(os.environ, {"VOLCENGINE_ARK_API_KEY": "shared-ark-key"}):
        assert module.resolve_provider_api_key("volcengine", "") == "shared-ark-key"
        assert module.resolve_provider_api_key("volcengine", "manual-override") == "manual-override"
        assert module.resolve_provider_api_key("t8star", "") == module.load_default_key()


def test_run_one_posts_synchronous_seedream_request_without_unsupported_seed(tmp_path):
    module = _load_nano()
    module.JOBS["job"] = {"events": []}
    fake_result = {"data": [{"b64_json": "aGVsbG8="}]}

    with mock.patch.object(module, "request_json", return_value=fake_result) as request, \
         mock.patch.object(
             module,
             "save_image_item",
             return_value=("", str(tmp_path / "seedream-test_1.png")),
         ):
        result = module.run_one(
            "job",
            1,
            _values(tmp_path, base_url="https://untrusted.example/steal"),
            {},
            "ws",
        )

    method, url, key, payload = request.call_args.args[:4]
    assert method == "POST"
    assert url == "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    assert key == "ark-key"
    assert payload == {
        "model": "doubao-seedream-5-0-pro-260628",
        "prompt": "一只纸雕小鸟",
        "size": "2048x1152",
        "response_format": "b64_json",
        "output_format": "png",
        "watermark": False,
    }
    assert result["status"] == "succeeded"
    assert result["task_id"].startswith("seedream_")


def test_seedream_rejects_more_than_ten_reference_images(tmp_path):
    module = _load_nano()
    module.JOBS["job"] = {"events": []}
    files = {f"image_{i}": (f"{i}.png", b"png") for i in range(1, 12)}

    with pytest.raises(ValueError, match="最多支持 10 张参考图"):
        module.run_one(
            "job",
            1,
            _values(tmp_path, mode="img2img", aspect_ratio="auto", image_size="2K"),
            files,
            "ws",
        )
