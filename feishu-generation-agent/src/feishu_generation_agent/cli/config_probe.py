import argparse
import asyncio
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from feishu_generation_agent.bootstrap import runtime_is_configured
from feishu_generation_agent.config import Settings
from feishu_generation_agent.integrations.bitable_url import parse_bitable_url
from feishu_generation_agent.integrations.feishu_bitable import FeishuBitableClient
from feishu_generation_agent.integrations.feishu_client import FeishuClient
from feishu_generation_agent.integrations.production_bitable import (
    ProductionBitableClient,
)


def _configured(settings: Settings, *names: str) -> bool:
    try:
        settings.require(*names)
    except ValueError:
        return False
    return True


def _result(
    configured: bool,
    *,
    reachable: bool | None = None,
    permission_ok: bool | None = None,
    message: str,
) -> dict[str, Any]:
    return {
        "configured": configured,
        "reachable": reachable,
        "permission_ok": permission_ok,
        "message": message,
    }


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix="agent-probe-", dir=path)
        os.close(descriptor)
        Path(filename).unlink()
    except OSError:
        return False
    return True


async def _http_probe(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    expected_model: str | None = None,
) -> tuple[bool, bool | None, str]:
    try:
        response = await client.request(method, url, headers=headers)
    except httpx.HTTPError:
        return False, False, "网络连接失败"
    if 200 <= response.status_code < 300:
        if expected_model is not None:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            items = []
            if isinstance(payload, dict):
                candidate = payload.get("data", payload.get("models"))
                if isinstance(candidate, list):
                    items = candidate
            if items:
                model_ids = {
                    str(item.get("id") or item.get("name", ""))
                    .removeprefix("models/")
                    .strip()
                    for item in items
                    if isinstance(item, dict)
                }
                if expected_model.removeprefix("models/") not in model_ids:
                    return True, False, "凭证有效，但配置的模型不在模型列表中"
        return True, True, "只读鉴权检查通过"
    if response.status_code in {401, 403}:
        return True, False, "凭证无效或权限不足"
    if response.status_code == 404:
        return True, False, "配置的模型不存在或不可见"
    if response.status_code == 429:
        return True, None, "服务可达但当前限流"
    return True, False, f"只读检查返回 HTTP {response.status_code}"


async def probe(settings: Settings, *, network: bool = True) -> dict[str, Any]:
    settings.ensure_paths()
    storage_ok = _writable(settings.data_dir) and _writable(settings.outputs_dir)
    checks: dict[str, dict[str, Any]] = {
        "local_storage": _result(
            storage_ok,
            reachable=True,
            permission_ok=storage_ok,
            message="本地目录可写" if storage_ok else "本地目录不可写",
        )
    }
    feishu_configured = _configured(settings, "lark_app_id", "lark_app_secret")
    feishu_reachable: bool | None = None
    feishu_permission: bool | None = None
    feishu_message = "缺少飞书应用凭证"
    feishu_client: FeishuClient | None = None
    if feishu_configured and network:
        feishu_client = FeishuClient(settings)
        try:
            await feishu_client.tenant_token()
        except Exception:
            feishu_reachable = False
            feishu_permission = False
            feishu_message = "飞书鉴权失败"
        else:
            feishu_reachable = True
            feishu_permission = True
            feishu_message = "tenant token 鉴权通过"
    elif feishu_configured:
        feishu_message = "已配置，跳过网络检查"
    checks["feishu_auth"] = _result(
        feishu_configured,
        reachable=feishu_reachable,
        permission_ok=feishu_permission,
        message=feishu_message,
    )
    for name, configured in {
        "feishu_docx_read": feishu_configured,
        "feishu_wiki_read": feishu_configured,
        "feishu_media_download": feishu_configured,
        "feishu_document_create": feishu_configured,
        "feishu_collaborator_write": _configured(
            settings, "lark_output_owner_open_id"
        ),
    }.items():
        checks[name] = _result(
            configured,
            reachable=feishu_reachable,
            permission_ok=None,
            message=(
                "需在专用测试文档上验证具体权限"
                if configured
                else "缺少配置"
            ),
        )

    bitable_configured = _configured(
        settings,
        "lark_app_id",
        "lark_app_secret",
        "lark_bitable_url",
        "lark_bitable_table_id",
        "lark_bitable_view_id",
    )
    if not bitable_configured:
        checks["bitable_schema"] = _result(False, message="缺少配置")
        checks["bitable_read"] = _result(False, message="缺少配置")
    elif not network:
        checks["bitable_schema"] = _result(
            True, message="已配置，跳过网络检查"
        )
        checks["bitable_read"] = _result(
            True, message="已配置，跳过网络检查"
        )
    elif not feishu_permission or feishu_client is None:
        checks["bitable_schema"] = _result(
            True,
            reachable=feishu_reachable,
            permission_ok=False,
            message="飞书鉴权失败，未执行字段检查",
        )
        checks["bitable_read"] = _result(
            True,
            reachable=feishu_reachable,
            permission_ok=False,
            message="飞书鉴权失败，未执行记录读取",
        )
    else:
        bitable = FeishuBitableClient(feishu_client)
        try:
            location = parse_bitable_url(
                settings.lark_bitable_url or "",
                settings.lark_bitable_table_id or "",
                settings.lark_bitable_view_id or "",
            )
            location = await bitable.resolve_location(location)
            schema = await bitable.ensure_schema(location)
        except Exception:
            checks["bitable_schema"] = _result(
                True,
                reachable=True,
                permission_ok=False,
                message="多维表格字段或权限检查失败",
            )
            checks["bitable_read"] = _result(
                True,
                reachable=True,
                permission_ok=False,
                message="字段检查未通过，未读取记录",
            )
        else:
            checks["bitable_schema"] = _result(
                True,
                reachable=True,
                permission_ok=True,
                message="四个既有字段检查通过",
            )
            try:
                await bitable.list_tasks(location, schema)
            except Exception:
                checks["bitable_read"] = _result(
                    True,
                    reachable=True,
                    permission_ok=False,
                    message="多维表格记录读取失败",
                )
            else:
                checks["bitable_read"] = _result(
                    True,
                    reachable=True,
                    permission_ok=True,
                    message="多维表格只读扫描通过",
                )

    production_configured = _configured(
        settings,
        "lark_app_id",
        "lark_app_secret",
        "lark_production_bitable_url",
        "lark_production_table_id",
        "lark_production_view_id",
        "lark_result_folder_token",
    )
    if not production_configured:
        checks["production_bitable_schema"] = _result(False, message="缺少配置")
        checks["production_bitable_read"] = _result(False, message="缺少配置")
        checks["result_bitable_write"] = _result(False, message="缺少配置")
    else:
        checks["result_bitable_write"] = _result(
            True,
            message="已配置；创建结果表与授权权限未验证（预检只读）",
        )
        if not network:
            checks["production_bitable_schema"] = _result(
                True, message="已配置，跳过网络检查"
            )
            checks["production_bitable_read"] = _result(
                True, message="已配置，跳过网络检查"
            )
        elif not feishu_permission or feishu_client is None:
            checks["production_bitable_schema"] = _result(
                True,
                reachable=feishu_reachable,
                permission_ok=False,
                message="飞书鉴权失败，未执行生产表字段检查",
            )
            checks["production_bitable_read"] = _result(
                True,
                reachable=feishu_reachable,
                permission_ok=False,
                message="飞书鉴权失败，未执行生产表读取",
            )
        else:
            production = ProductionBitableClient(feishu_client)
            try:
                location = parse_bitable_url(
                    settings.lark_production_bitable_url or "",
                    settings.lark_production_table_id or "",
                    settings.lark_production_view_id or "",
                )
                location = await production.resolve_location(location)
                schema = await production.ensure_schema(location)
            except Exception:
                checks["production_bitable_schema"] = _result(
                    True,
                    reachable=True,
                    permission_ok=False,
                    message="生产表字段或权限检查失败",
                )
                checks["production_bitable_read"] = _result(
                    True,
                    reachable=True,
                    permission_ok=False,
                    message="字段检查未通过，未读取生产表记录",
                )
            else:
                checks["production_bitable_schema"] = _result(
                    True,
                    reachable=True,
                    permission_ok=True,
                    message="生产表六个字段检查通过",
                )
                try:
                    await production.list_tasks(
                        location,
                        schema,
                        include_completed=settings.lark_include_completed_for_test,
                    )
                except Exception:
                    checks["production_bitable_read"] = _result(
                        True,
                        reachable=True,
                        permission_ok=False,
                        message="生产表记录读取失败",
                    )
                else:
                    checks["production_bitable_read"] = _result(
                        True,
                        reachable=True,
                        permission_ok=True,
                        message="生产表只读扫描通过",
                    )

    if feishu_client is not None:
        await feishu_client.close()

    endpoints = {
        "deepseek": (
            _configured(settings, "deepseek_api_key", "deepseek_model"),
            f"{settings.deepseek_base_url.rstrip('/')}/models",
            {
                "Authorization": "Bearer "
                + (
                    settings.deepseek_api_key.get_secret_value()
                    if settings.deepseek_api_key
                    else ""
                )
            },
            settings.deepseek_model,
        ),
        "claude_vision": (
            _configured(settings, "claude_api_key", "claude_model"),
            f"{(settings.claude_base_url or 'https://api.anthropic.com').rstrip('/')}/v1/models/{settings.claude_model or 'missing'}",
            {
                "x-api-key": (
                    settings.claude_api_key.get_secret_value()
                    if settings.claude_api_key
                    else ""
                ),
                "anthropic-version": "2023-06-01",
            },
            None,
        ),
        "chiyun": (
            _configured(settings, "chiyun_api_key", "chiyun_model"),
            f"{settings.chiyun_base_url.rstrip('/')}/v1beta/models",
            {
                "Authorization": "Bearer "
                + (
                    settings.chiyun_api_key.get_secret_value()
                    if settings.chiyun_api_key
                    else ""
                )
            },
            settings.chiyun_model,
        ),
        "seedance": (
            _configured(settings, "ark_api_key", "seedance_model"),
            f"{settings.ark_base_url.rstrip('/')}/models",
            {
                "Authorization": "Bearer "
                + (
                    settings.ark_api_key.get_secret_value()
                    if settings.ark_api_key
                    else ""
                )
            },
            settings.seedance_model,
        ),
    }
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        for name, (configured, url, headers, expected_model) in endpoints.items():
            if not configured:
                checks[name] = _result(False, message="缺少配置")
            elif not network:
                checks[name] = _result(True, message="已配置，跳过网络检查")
            else:
                reachable, permission, message = await _http_probe(
                    client,
                    "GET",
                    url,
                    headers=headers,
                    expected_model=expected_model,
                )
                checks[name] = _result(
                    True,
                    reachable=reachable,
                    permission_ok=permission,
                    message=message,
                )
    configured = runtime_is_configured(settings)
    reachable_values = [
        item["reachable"] for item in checks.values() if item["configured"]
    ]
    return {
        "ready": configured
        and storage_ok
        and all(value is not False for value in reachable_values),
        "capabilities": checks,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检查本地目录、凭证配置和只读接口可达性"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--network",
        action="store_true",
        help="显式执行只读飞书、多维表格和模型鉴权检查（默认行为）",
    )
    mode.add_argument(
        "--no-network",
        action="store_true",
        help="只检查本地配置，不访问外部接口",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = asyncio.run(probe(Settings(), network=not args.no_network))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
