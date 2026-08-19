"""Ark 私域人像素材库适配器（同步版）。

把画布上传的人像图传进火山方舟私域资产库（AIGC 分组），画布侧以
素材库资产形式引用。生成时上游用 asset://<id> 引用 —— 方舟官方的人像素材用法；
我们的链路由翻译层从本地副本取字节，效果等价（见 translate.py）。

协议与上游 server/ai_creation_canvas/adapters/ark_assets.py 对齐：
TOS SigV4 PUT 传对象 → TOS 预签名 GET URL → OpenAPI v4 签名 CreateAsset
→ GetAsset 轮询状态。全部同步实现（httpx.Client + time.sleep），
因为我们的子应用后端是同步的 stdlib 风格。

配置：infinite-canvas/state/asset-library.json（gitignored），
模板见 infinite-canvas/config/asset-library.example.json。
SK 可能是控制台复制出来的 base64，读配置时规范化（normalize_secret_key）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

STATE_DIR = Path(__file__).resolve().parent / "state"
CONFIG_PATH = STATE_DIR / "asset-library.json"
GROUP_STATE_PATH = STATE_DIR / "asset-library.group.json"

_HOST = "ark.cn-beijing.volcengineapi.com"
_VERSION = "2024-01-01"
_ARK_ASSET_ID = re.compile(r"asset-[A-Za-z0-9_-]{1,100}\Z")
_GROUP_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_IMAGE_MIMES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_DEFAULT_GROUP_NAME = "canvas-aigc-default"
_PRESIGNED_EXPIRES = 43200
_STATUSES = {"Processing": "processing", "Active": "active", "Failed": "failed"}


class LibraryError(Exception):
    """素材库上游不可用/被拒。retryable 决定对外返 502 还是 422。"""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class LibraryInvalid(Exception):
    """上游返回了无法解析的响应。"""


# ------------------------------------------------------------------- 配置

def normalize_secret_key(value: str) -> str:
    """控制台复制出的 SK 是 base64，解码成明文；明文则原样返回。"""
    try:
        decoded = base64.b64decode(value.encode("utf-8"), validate=True)
        if decoded and base64.b64encode(decoded) == value.encode("utf-8"):
            text = decoded.decode("utf-8")
            if text.isprintable() and not any(ord(c) < 32 or ord(c) == 127 for c in text):
                return text
    except (ValueError, UnicodeError):
        pass
    return value


def load_config() -> dict | None:
    """读取并校验素材库配置。缺失或非法一律返回 None —— 调用方按 503 处理。"""
    try:
        raw = CONFIG_PATH.read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    for field in ("ark_access_key", "ark_secret_key", "tos_access_key",
                  "tos_secret_key", "tos_bucket", "tos_region"):
        value = data.get(field)
        if not isinstance(value, str) or not value or not value.strip():
            return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,62}", data["tos_bucket"]):
        return None
    if not re.fullmatch(r"[a-z0-9-]{2,32}", data["tos_region"]):
        return None
    cfg = {
        "ark_access_key": data["ark_access_key"].strip(),
        "ark_secret_key": normalize_secret_key(data["ark_secret_key"]),
        "tos_access_key": data["tos_access_key"].strip(),
        "tos_secret_key": normalize_secret_key(data["tos_secret_key"]),
        "tos_bucket": data["tos_bucket"],
        "tos_region": data["tos_region"],
        "project_name": str(data.get("project_name") or "Seedance2.0")[:64],
    }
    return cfg


def _group_id() -> str | None:
    try:
        data = json.loads(GROUP_STATE_PATH.read_bytes())
    except (OSError, ValueError):
        return None
    value = data.get("group_id") if isinstance(data, dict) else None
    if isinstance(value, str) and _GROUP_ID.fullmatch(value):
        return value
    return None


def _save_group_id(group_id: str) -> None:
    GROUP_STATE_PATH.write_text(json.dumps({"group_id": group_id}))


# -------------------------------------------------------------------- 签名

def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _amz_date(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(secret.encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "request")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _openapi_signed_headers(cfg: dict, action: str, payload: dict) -> tuple[str, dict]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    query = f"Action={action}&Version={_VERSION}"
    now = _now()
    amz_date = _amz_date(now)
    date_stamp = amz_date[:8]
    payload_hash = _sha256_hex(body)
    headers = {"Host": _HOST, "X-Date": amz_date, "X-Content-Sha256": payload_hash,
               "Content-Type": "application/json"}
    canonical_headers = "".join(f"{k.lower()}:{headers[k].strip()}\n" for k in sorted(headers, key=str.lower))
    signed_headers = ";".join(k.lower() for k in sorted(headers, key=str.lower))
    canonical_request = f"POST\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    credential_scope = f"{date_stamp}/cn-beijing/ark/request"
    string_to_sign = f"HMAC-SHA256\n{amz_date}\n{credential_scope}\n{_sha256_hex(canonical_request)}"
    signature = hmac.new(_signing_key(cfg["ark_secret_key"], date_stamp, "cn-beijing", "ark"),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (f"HMAC-SHA256 Credential={cfg['ark_access_key']}/{credential_scope}, "
                     f"SignedHeaders={signed_headers}, Signature={signature}")
    return query, {**headers, "Authorization": authorization}


def _tos_signed_put(cfg: dict, object_key: str, mime: str, body: bytes) -> dict:
    host = f"{cfg['tos_bucket']}.tos-{cfg['tos_region']}.volces.com"
    now = _now()
    amz_date = _amz_date(now)
    date_stamp = amz_date[:8]
    payload_hash = _sha256_hex(body)
    headers = {"Host": host, "Content-Type": mime, "x-tos-content-sha256": payload_hash,
               "x-tos-date": amz_date}
    signed = sorted(headers, key=str.lower)
    canonical_headers = "".join(f"{k.lower()}:{headers[k].strip()}\n" for k in signed)
    signed_headers = ";".join(k.lower() for k in signed)
    canonical_uri = "/" + quote(object_key, safe="/")
    canonical_request = f"PUT\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    credential_scope = f"{date_stamp}/{cfg['tos_region']}/tos/request"
    string_to_sign = f"TOS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{_sha256_hex(canonical_request.encode('utf-8'))}"
    signature = hmac.new(_signing_key(cfg["tos_secret_key"], date_stamp, cfg["tos_region"], "tos"),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (f"TOS4-HMAC-SHA256 Credential={cfg['tos_access_key']}/{credential_scope}, "
                                f"SignedHeaders={signed_headers}, Signature={signature}")
    return headers


def _tos_presigned_get_url(cfg: dict, object_key: str) -> str:
    host = f"{cfg['tos_bucket']}.tos-{cfg['tos_region']}.volces.com"
    now = _now()
    amz_date = _amz_date(now)
    date_stamp = amz_date[:8]
    credential_scope = f"{date_stamp}/{cfg['tos_region']}/tos/request"
    credential = f"{cfg['tos_access_key']}/{credential_scope}"
    values = {"X-Tos-Algorithm": "TOS4-HMAC-SHA256", "X-Tos-Credential": credential,
              "X-Tos-Date": amz_date, "X-Tos-Expires": str(_PRESIGNED_EXPIRES),
              "X-Tos-SignedHeaders": "host"}
    canonical_query = "&".join(f"{quote(k, safe='')}={quote(values[k], safe='')}" for k in sorted(values))
    canonical_uri = "/" + quote(object_key, safe="/")
    canonical_request = f"GET\n{canonical_uri}\n{canonical_query}\nhost:{host}\nhost\nUNSIGNED-PAYLOAD"
    string_to_sign = f"TOS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{_sha256_hex(canonical_request.encode('utf-8'))}"
    signature = hmac.new(_signing_key(cfg["tos_secret_key"], date_stamp, cfg["tos_region"], "tos"),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"https://{host}{canonical_uri}?{canonical_query}&X-Tos-Signature={signature}"


# ------------------------------------------------------------- OpenAPI 调用

def _openapi_call(cfg: dict, action: str, payload: dict) -> dict:
    query, headers = _openapi_signed_headers(cfg, action, payload)
    try:
        with httpx.Client(timeout=600, follow_redirects=False, trust_env=False) as client:
            response = client.post(f"https://{_HOST}/?{query}", headers=headers,
                                   content=json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    except httpx.HTTPError as exc:
        raise LibraryError("素材库服务不可用。", retryable=True) from exc
    if response.status_code in {408, 429} or response.status_code >= 500:
        raise LibraryError("素材库服务不可用。", retryable=True)
    if response.status_code < 200 or response.status_code >= 300:
        raise LibraryError("素材库拒绝了该请求。")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise LibraryInvalid("素材库返回了无法解析的响应。")
    try:
        value = response.json()
    except ValueError as exc:
        raise LibraryInvalid("素材库返回了无法解析的响应。") from exc
    if not isinstance(value, dict):
        raise LibraryInvalid("素材库返回了无法解析的响应。")
    return value


# ---------------------------------------------------------------- 素材操作

def ensure_default_group(cfg: dict) -> str:
    """已有持久化的分组直接复用，否则创建 AIGC 分组并落盘。"""
    existing = _group_id()
    if existing is not None:
        return existing
    data = _openapi_call(cfg, "CreateAssetGroup",
                         {"Name": _DEFAULT_GROUP_NAME, "ProjectName": cfg["project_name"],
                          "GroupType": "AIGC"})
    result = data.get("Result")
    if not isinstance(result, dict):
        raise LibraryInvalid("素材库分组响应无效。")
    identifier = result.get("Id") or result.get("GroupId")
    if not isinstance(identifier, str) or _GROUP_ID.fullmatch(identifier) is None:
        raise LibraryInvalid("素材库分组响应无效。")
    _save_group_id(identifier)
    return identifier


def upload_image(cfg: dict, path: str, mime: str, size: int, filename: str) -> tuple[str, str]:
    """上传一张人像图到素材库。返回 (upstream_asset_id, status)。"""
    if mime not in _IMAGE_MIMES or not (1 <= size <= _MAX_IMAGE_BYTES):
        raise ValueError("素材库只接受 10MB 以内的 PNG/JPEG/WebP 图片。")
    blob = Path(path).read_bytes()
    if len(blob) != size:
        raise ValueError("素材文件在上传过程中发生了变化。")
    group_id = ensure_default_group(cfg)

    object_key = f"refmedia/{uuid.uuid4().hex}{_IMAGE_MIMES[mime]}"
    signed = _tos_signed_put(cfg, object_key, mime, blob)
    try:
        with httpx.Client(timeout=600, follow_redirects=False, trust_env=False) as client:
            response = client.put(f"https://{signed['Host']}/{quote(object_key, safe='/')}",
                                  headers=signed, content=blob)
    except httpx.HTTPError as exc:
        raise LibraryError("素材库服务不可用。", retryable=True) from exc
    if response.status_code not in {200, 201}:
        raise LibraryError("素材库服务不可用。",
                           retryable=response.status_code in {408, 429} or response.status_code >= 500)
    public_url = _tos_presigned_get_url(cfg, object_key)

    name = "".join(c for c in Path(filename).stem if ord(c) >= 32 and ord(c) != 127)[:64] or "portrait"
    data = _openapi_call(cfg, "CreateAsset",
                         {"GroupId": group_id, "URL": public_url, "AssetType": "Image",
                          "ProjectName": cfg["project_name"], "Name": name})
    result = data.get("Result")
    if not isinstance(result, dict):
        raise LibraryInvalid("素材库响应无效。")
    asset_id = result.get("Id")
    if not isinstance(asset_id, str) or _ARK_ASSET_ID.fullmatch(asset_id) is None:
        raise LibraryInvalid("素材库响应无效。")
    return asset_id, poll_status(cfg, asset_id)


def poll_status(cfg: dict, asset_id: str) -> str:
    """轮询 GetAsset 直到离开 Processing。30 次 × 1 秒后仍 Processing 就如实返回。"""
    for attempt in range(30):
        data = _openapi_call(cfg, "GetAsset",
                             {"Id": asset_id, "ProjectName": cfg["project_name"]})
        result = data.get("Result")
        if not isinstance(result, dict) or result.get("Id") != asset_id:
            raise LibraryInvalid("素材库状态响应无效。")
        status = _STATUSES.get(result.get("Status"))
        if status is None:
            raise LibraryInvalid("素材库状态响应无效。")
        if status != "processing":
            return status
        if attempt + 1 < 30:
            time.sleep(1)
    return "processing"


def get_asset_status(cfg: dict, asset_id: str) -> str:
    data = _openapi_call(cfg, "GetAsset",
                         {"Id": asset_id, "ProjectName": cfg["project_name"]})
    result = data.get("Result")
    if not isinstance(result, dict) or result.get("Id") != asset_id:
        raise LibraryInvalid("素材库状态响应无效。")
    status = _STATUSES.get(result.get("Status"))
    if status is None:
        raise LibraryInvalid("素材库状态响应无效。")
    return status
