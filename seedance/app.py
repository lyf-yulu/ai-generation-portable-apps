#!/usr/bin/env python3
from __future__ import annotations

import base64
import cgi
import concurrent.futures
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
_DATA_BASE = Path(os.environ.get("DATA_DIR", str(ROOT)))
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = _DATA_BASE / "outputs"
STATE_DIR = _DATA_BASE / "state"
MEDIA_DIR = STATE_DIR / "media"
PRESET_PATH = STATE_DIR / "preset.json"
ACTIVITY_PATH = STATE_DIR / "activity_log.json"
ARCHIVE_DIR = _DATA_BASE / "archives"
PROVIDERS_PATH = ROOT / "providers.json"
FILES_MAP_PATH = STATE_DIR / "download_files.json"
SKILL_PATH = ROOT / "SKILL.md"
DEEPSEEK_KEY_PATH = STATE_DIR / "deepseek.key"
SECRETS_PATH = STATE_DIR / "secrets.json"


def _safe_join_or_root(base: Path, rel: str) -> str:
    """Join base/rel and reject any result outside base (path-traversal guard).

    Falls back to returning base itself for illegal input; SimpleHTTPRequestHandler
    then serves a directory listing (or 403 if listing disabled), which is a safer
    failure mode than serving arbitrary files."""
    try:
        base_resolved = base.resolve()
        target = (base / rel).resolve()
    except (OSError, ValueError):
        return str(base)
    if target == base_resolved or target.is_relative_to(base_resolved):
        return str(target)
    return str(base)


# Magic bytes for common formats; used to verify uploads instead of trusting the
# client's declared extension / Content-Type. An attacker who uploads evil.jpg
# with SVG-plus-<script> body would slip past extension checks and (without
# nosniff) execute in the victim's browser.
_MAGIC_IMAGE = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/gif": [b"GIF87a", b"GIF89a"],
    "image/webp": [b"RIFF"],  # + "WEBP" at offset 8; checked below
    "image/bmp": [b"BM"],
    "image/tiff": [b"II*\x00", b"MM\x00*"],
    "image/heic": [b"ftypheic", b"ftypheix", b"ftypmif1"],  # matched at offset 4
}
_MAGIC_VIDEO = {
    "video/mp4": [b"ftypmp4", b"ftypisom", b"ftypM4V", b"ftypavc1"],  # offset 4
    "video/quicktime": [b"ftypqt"],  # offset 4
    "video/webm": [b"\x1a\x45\xdf\xa3"],
}
_MAGIC_AUDIO = {
    "audio/mpeg": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
    "audio/wav": [b"RIFF"],  # + "WAVE" at offset 8
    "audio/ogg": [b"OggS"],
}


def sniff_kind(head: bytes) -> str | None:
    """Return the top-level media kind ('image' | 'video' | 'audio') detected
    from the file header, or None if unrecognized. Uses first ~16 bytes so
    callers should pass at least that many."""
    if not head:
        return None
    for magics in _MAGIC_IMAGE.values():
        for m in magics:
            if head.startswith(m):
                if m == b"RIFF" and head[8:12] != b"WEBP":
                    continue
                return "image"
        # heic/tiff family: bytes 4-11 marker
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image"
    if head[4:8] in (b"ftyp",) and head[8:12] in (b"heic", b"heix", b"mif1"):
        return "image"
    for magics in _MAGIC_VIDEO.values():
        for m in magics:
            if m.startswith(b"ftyp"):
                if head[4:4 + len(m)] == m:
                    return "video"
            elif head.startswith(m):
                return "video"
    for magics in _MAGIC_AUDIO.values():
        for m in magics:
            if head.startswith(m):
                if m == b"RIFF" and head[8:12] != b"WAVE":
                    continue
                return "audio"
    return None


def load_secrets() -> dict[str, str]:
    """Server-managed API keys (currently: volcengine).
    Fail-fast: missing file or empty required keys raises at import time so the
    portal watchdog surfaces the sub-app as crashed and the operator notices.
    """
    if not SECRETS_PATH.exists():
        raise RuntimeError(
            f"secrets file missing: {SECRETS_PATH}. "
            "Copy seedance/secrets.example.json to seedance/state/secrets.json "
            "and fill 'volcengine_api_key'."
        )
    try:
        data = json.loads(SECRETS_PATH.read_text("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"secrets file {SECRETS_PATH} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"secrets file {SECRETS_PATH} must be a JSON object")
    if not (data.get("volcengine_api_key") or "").strip():
        raise RuntimeError(
            f"'volcengine_api_key' missing or empty in {SECRETS_PATH}"
        )
    return data


SECRETS = load_secrets()


# ── Volcengine TOS upload (for video/audio reference media in Ark tasks) ─────
# Portal injects AK/SK via env from volcengine-portrait/config.json so seedance
# inherits the same company credentials. tos_bucket / tos_region live in
# secrets.json (operator-managed). Both pieces must be present for video/audio
# refs to work — TOS upload raises a clear RuntimeError when anything is missing.
TOS_ACCESS_KEY = os.environ.get("TOS_ACCESS_KEY", "").strip()
TOS_SECRET_KEY = os.environ.get("TOS_SECRET_KEY", "").strip()
TOS_DEFAULT_REGION = "cn-beijing"


def _tos_sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tos_hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _tos_sign_put(bucket: str, region: str, object_key: str, mime: str, body: bytes) -> dict[str, str]:
    """Build SigV4-style headers for a TOS PutObject request. TOS uses the
    `TOS4-HMAC-SHA256` algorithm and the `x-tos-*` header convention — same
    derivation steps as AWS SigV4 but different identifiers."""
    host = f"{bucket}.tos-{region}.volces.com"
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    payload_hash = _tos_sha256_hex(body)

    headers = {
        "Host": host,
        "Content-Type": mime,
        "x-tos-content-sha256": payload_hash,
        "x-tos-date": amz_date,
    }

    # Canonical headers (lowercase name, sorted by name)
    signed = sorted(headers.keys(), key=str.lower)
    canonical_headers = "".join(f"{k.lower()}:{headers[k].strip()}\n" for k in signed)
    signed_headers = ";".join(k.lower() for k in signed)

    canonical_uri = "/" + urllib.parse.quote(object_key, safe="/")
    canonical_request = (
        f"PUT\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    credential_scope = f"{date_stamp}/{region}/tos/request"
    string_to_sign = (
        f"TOS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{_tos_sha256_hex(canonical_request.encode('utf-8'))}"
    )

    k_date = _tos_hmac(TOS_SECRET_KEY.encode("utf-8"), date_stamp)
    k_region = _tos_hmac(k_date, region)
    k_service = _tos_hmac(k_region, "tos")
    k_signing = _tos_hmac(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"TOS4-HMAC-SHA256 Credential={TOS_ACCESS_KEY}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers["Content-Length"] = str(len(body))
    return headers


def _tos_presigned_get_url(bucket: str, region: str, object_key: str, expires: int = 43200) -> str:
    """Generate a query-string-signed GET URL for a private TOS object.
    Default TTL = 12 hours (covers any reasonable Ark generation cycle)."""
    host = f"{bucket}.tos-{region}.volces.com"
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    credential_scope = f"{date_stamp}/{region}/tos/request"
    credential = f"{TOS_ACCESS_KEY}/{credential_scope}"

    qs = {
        "X-Tos-Algorithm": "TOS4-HMAC-SHA256",
        "X-Tos-Credential": credential,
        "X-Tos-Date": amz_date,
        "X-Tos-Expires": str(expires),
        "X-Tos-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(qs[k], safe='')}"
        for k in sorted(qs)
    )
    canonical_uri = "/" + urllib.parse.quote(object_key, safe="/")
    canonical_headers = f"host:{host}\n"
    canonical_request = (
        f"GET\n{canonical_uri}\n{canonical_query}\n{canonical_headers}\nhost\nUNSIGNED-PAYLOAD"
    )
    string_to_sign = (
        f"TOS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{_tos_sha256_hex(canonical_request.encode('utf-8'))}"
    )

    k_date = _tos_hmac(TOS_SECRET_KEY.encode("utf-8"), date_stamp)
    k_region = _tos_hmac(k_date, region)
    k_service = _tos_hmac(k_region, "tos")
    k_signing = _tos_hmac(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return f"https://{host}{canonical_uri}?{canonical_query}&X-Tos-Signature={signature}"


def _ext_from_mime(mime: str) -> str:
    guess = mimetypes.guess_extension(mime or "")
    return guess or ".bin"


def tos_upload(blob: bytes, mime: str, filename: str) -> str:
    """Upload blob to the configured TOS bucket and return its public https URL.
    Raises RuntimeError with a user-readable message on any precondition or
    transport failure. The bucket must be configured as public-read so Ark can
    GET the resulting URL without authentication."""
    if not (TOS_ACCESS_KEY and TOS_SECRET_KEY):
        raise RuntimeError(
            "TOS 凭证未配置：请在 Portal 管理员菜单 →「火山方舟人像 Key」处配置 AK/SK，"
            "重启 Portal 后 seedance 会自动继承同一对 Key"
        )
    bucket = (SECRETS.get("tos_bucket") or "").strip()
    if not bucket:
        raise RuntimeError(
            "seedance/state/secrets.json 缺 'tos_bucket'：请在火山 TOS 控制台创建一个"
            "公共读 bucket，把 bucket 名填进去后重启"
        )
    region = (SECRETS.get("tos_region") or TOS_DEFAULT_REGION).strip()
    ext = Path(filename).suffix if filename else ""
    if not ext:
        ext = _ext_from_mime(mime)
    object_key = f"refmedia/{uuid.uuid4().hex}{ext}"
    host = f"{bucket}.tos-{region}.volces.com"

    headers = _tos_sign_put(bucket, region, object_key, mime, blob)
    conn = http.client.HTTPSConnection(host, timeout=300)
    try:
        try:
            conn.request("PUT", "/" + object_key, body=blob, headers=headers)
            resp = conn.getresponse()
        except RuntimeError:
            raise
        except Exception as exc:
            # DNS resolution / TCP connect / TLS handshake / socket timeout —
            # wrap so the caller gets a clear 中文 message instead of a raw
            # network exception.
            raise RuntimeError(f"TOS 上传连接失败: {type(exc).__name__}: {exc}") from exc
        if resp.status not in (200, 201):
            body_err = resp.read()[:500].decode("utf-8", errors="replace")
            raise RuntimeError(f"TOS upload HTTP {resp.status}: {body_err}")
        resp.read()
    finally:
        conn.close()
    # Bucket is private; return a 12-hour presigned GET URL so Ark can fetch it
    # anonymously. Plenty of margin for any reasonable generation cycle.
    return _tos_presigned_get_url(bucket, region, object_key, expires=43200)


def _load_deepseek_key() -> str:
    """Resolution order: env var → state/deepseek.key (preferred for persistent
    config that survives restarts and isn't checked into git) → empty."""
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        if DEEPSEEK_KEY_PATH.exists():
            return DEEPSEEK_KEY_PATH.read_text("utf-8").strip()
    except Exception:
        pass
    return ""

try:
    SEEDANCE_SKILL = SKILL_PATH.read_text(encoding="utf-8").strip()
except Exception:
    SEEDANCE_SKILL = ""


PORTAL_SIG_WINDOW = int(os.environ.get("PORTAL_SIG_WINDOW", "60"))


def _verify_portal_sig(handler) -> bool:
    """True iff Portal's HMAC signature over (ts, is_admin, username) matches.

    Prevents a client from setting X-Is-Admin: 1 directly and bypassing auth.
    Timestamp guards against replay of an old signed request."""
    secret = os.environ.get("PORTAL_INTERNAL_TOKEN", "")
    if not secret:
        return False
    sig = handler.headers.get("X-Portal-Sig") or ""
    ts_raw = handler.headers.get("X-Portal-Ts") or ""
    if not sig or not ts_raw:
        return False
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > PORTAL_SIG_WINDOW:
        return False
    username = handler.headers.get("X-Username", "") or ""
    is_admin_flag = "1" if handler.headers.get("X-Is-Admin") == "1" else "0"
    msg = f"{ts}:{is_admin_flag}:{username}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _is_admin(handler: SimpleHTTPRequestHandler) -> bool:
    """Check if request comes from an authenticated admin (set by portal proxy).

    Requires a valid X-Portal-Sig — an unauthenticated LAN client that just
    sets X-Is-Admin: 1 will fail verification and be treated as a regular user."""
    return handler.headers.get("X-Is-Admin") == "1" and _verify_portal_sig(handler)


def _view_scope(handler) -> tuple[bool, str]:
    """Returns (sees_all, username). Used by jobs/activity endpoints to
    enforce per-user task visibility. Admins see everything; everyone else
    sees only jobs whose `username` matches."""
    sees_all = _is_admin(handler)
    username = _decode_username(handler)
    return sees_all, username


_USER_SANITIZE_RE = re.compile(r"[^\w\-一-鿿]+")


def _sanitize_username(name: str | None) -> str:
    """Compress a raw username into a safe directory name:
    keep letters/digits/underscore/hyphen/CJK, replace others with `_`,
    strip leading `.` `_`, cap at 40 chars, default to `unknown`."""
    s = _USER_SANITIZE_RE.sub("_", (name or "").strip())
    s = s.strip("._") or "unknown"
    return s[:40]


def _user_day_subdir(base: Path, username: str | None, day: str | None = None) -> Path:
    """Return (and create) `base/<sanitized_user>/<YYYY-MM-DD>/`."""
    user = _sanitize_username(username)
    d = day or time.strftime("%Y-%m-%d")
    p = base / user / d
    p.mkdir(parents=True, exist_ok=True)
    return p


def _decode_username(handler) -> str:
    """Portal injects X-Username via urllib.parse.quote() to survive the
    latin-1 limit of http.client headers; decode back to unicode here."""
    raw = (handler.headers.get("X-Username", "") or "").strip()
    if not raw:
        return ""
    try:
        return urllib.parse.unquote(raw)
    except Exception:
        return raw


APP_NAME = "seedance"
PORTAL_INTERNAL_TOKEN = os.environ.get("PORTAL_INTERNAL_TOKEN", "")
PORTAL_PORT_FOR_CALLBACK = int(os.environ.get("PORTAL_PORT", "9090"))
# Portal serves HTTPS-only with a self-signed cert; this loopback callback
# must skip cert verification.
import ssl as _ssl
_PORTAL_SSL_CTX = _ssl.create_default_context()
_PORTAL_SSL_CTX.check_hostname = False
_PORTAL_SSL_CTX.verify_mode = _ssl.CERT_NONE


def report_final_to_portal(job_id: str, status: str) -> None:
    """Fire-and-forget callback: tell portal this job reached terminal state.
    Portal rolls back the +1 stat counter on failure. Any error is swallowed
    so the task's own result path is never affected."""
    if not PORTAL_INTERNAL_TOKEN or not job_id:
        return
    try:
        payload = json.dumps({"app": APP_NAME, "job_id": job_id, "status": status}).encode("utf-8")
        req = urllib.request.Request(
            f"https://127.0.0.1:{PORTAL_PORT_FOR_CALLBACK}/api/internal/jobs/finalize",
            data=payload,
            headers={"X-Internal-Token": PORTAL_INTERNAL_TOKEN, "Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2, context=_PORTAL_SSL_CTX).read()
    except Exception:
        pass


def _workspace_id(handler) -> str:
    """Extract workspace_id: 1) X-Workspace-Id header  2) ?ws= query  3) localhost."""
    ws = (handler.headers.get("X-Workspace-Id") or "").strip()
    if ws:
        return re.sub(r"[^a-zA-Z0-9_\-]", "_", ws)[:64]
    # Fallback to query parameter (read from raw path since self.path is stripped of query in do_GET/do_POST)
    raw = getattr(handler, "_raw_path", None) or handler.path
    qs = urllib.parse.urlparse(raw).query
    params = urllib.parse.parse_qs(qs)
    if "ws" in params:
        return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(params["ws"][0]))[:64]
    return "localhost"


def _ws_dir(ws_id: str) -> Path:
    return STATE_DIR / "workspaces" / ws_id


def _ws_media_dir(ws_id: str) -> Path:
    return _ws_dir(ws_id) / "media"


def _ws_preset_path(ws_id: str) -> Path:
    return _ws_dir(ws_id) / "preset.json"


OFFICIAL_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
TERMINAL_STATUSES = {"succeeded", "success", "failed", "fail", "failure", "cancelled", "canceled"}

JOBS: dict[str, dict[str, Any]] = {}
FILES: dict[str, Path] = {}
JOBS_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
ACTIVITY_LIMIT = 100

# Reference media for volcengine Ark generation tasks. Ark requires video/audio
# reference URLs to be publicly downloadable web URLs (rejects data: URLs and
# Authenticated Ark /files paths). We stash the blob in state/refmedia/ and
# expose it via an anonymous /api/refmedia/<token> endpoint so the public
# entrypoint (cloudflared / HTTPS) can serve it back to Ark. Tokens are
# unguessable hex UUIDs and self-expire after REFMEDIA_TTL seconds.
REFMEDIA_DIR = STATE_DIR / "refmedia"
REFMEDIA_TTL = 3600
REFMEDIA: dict[str, dict[str, Any]] = {}
REFMEDIA_LOCK = threading.Lock()


def load_files_map() -> dict[str, Path]:
    """Load persisted download-token → file-path mapping from disk."""
    try:
        if FILES_MAP_PATH.exists():
            data = json.loads(FILES_MAP_PATH.read_text(encoding="utf-8"))
            result: dict[str, Path] = {}
            for token, path_str in data.items():
                p = Path(path_str)
                if p.exists():
                    result[token] = p
            return result
    except Exception:
        pass
    return {}


def save_files_map() -> None:
    """Persist the current FILES mapping to disk atomically."""
    try:
        with JOBS_LOCK:
            data = {token: str(p) for token, p in FILES.items()}
        _atomic_write(FILES_MAP_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _atomic_write(path: Path, content: str):
    """Thread-safe atomic write: tmp → rename."""
    with STATE_LOCK:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

# ---- Client IP helpers ----

def _client_ip(handler: SimpleHTTPRequestHandler) -> str:
    """Extract real client IP. Behind portal proxy, use X-Forwarded-For.
    Otherwise use the direct connection address.
    Returns a safe slug usable as a directory name."""
    xff = handler.headers.get("X-Forwarded-For", "").strip()
    if xff:
        ip = xff.split(",")[0].strip()
        if ip:
            return re.sub(r"[^0-9a-fA-F.:]+", "_", ip)
    addr = handler.client_address[0] if handler.client_address else "127.0.0.1"
    return re.sub(r"[^0-9a-fA-F.:]+", "_", addr)

def _archive_dir_for(handler_or_ip: Any) -> Path:
    """Return archive subdir. Prefers <username>/<date>/; falls back to IP for
    string inputs (legacy call sites)."""
    if hasattr(handler_or_ip, "headers"):
        user = _decode_username(handler_or_ip)
        return _user_day_subdir(ARCHIVE_DIR, user)
    # Legacy string path: keep old behavior so read side still finds old archives
    return ARCHIVE_DIR / _client_ip(handler_or_ip)

FILE_FIELDS = {
    "first_frame",
    "last_frame",
    *{f"ref_image_{i}" for i in range(1, 10)},
    *{f"ref_video_{i}" for i in range(1, 4)},
    *{f"ref_audio_{i}" for i in range(1, 4)},
}
VALUE_FIELDS = {
    "api_key",
    "provider",
    "base_url",
    "output_dir",
    "model",
    "custom_model",
    "duration",
    "resolution",
    "ratio",
    "seed",
    "generate_audio",
    "watermark",
    "return_last_frame",
    "web_search",
    "repeat_count",
    "concurrency",
    "poll_interval",
    "timeout",
    "vary_seed",
    "output_name",
    "prompt",
}

FALLBACK_PROVIDERS = {
    "schema_version": 1,
    "app": "seedance",
    "default_provider": "volcengine",
    "providers": {
        "volcengine": {
            "label": "豆包官方 / 火山方舟",
            "base_url": OFFICIAL_ARK_BASE_URL,
            "api_style": "ark_seedance",
            "hint": "豆包官方火山方舟 API。本地图/视频/音频参考素材会先上传到公司 TOS bucket，再以预签名 URL 传给方舟。",
            "defaults": {"model": "doubao-seedance-2-0-260128", "duration": 12, "resolution": "720p", "ratio": "16:9", "repeat_count": 1, "concurrency": 1, "poll_interval": 10, "timeout": 3600, "vary_seed": True},
            "models": [{"id": "doubao-seedance-2-0-260128", "label": "doubao-seedance-2-0-260128"}, {"id": "doubao-seedance-2-0-fast-260128", "label": "doubao-seedance-2-0-fast-260128"}],
        },
    },
}


def json_response(handler: SimpleHTTPRequestHandler, status: int, data: dict[str, Any]) -> None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    # Surface job_id on a header so the upstream proxy can register usage stats
    # without buffering the body (P0 fix for #15 portal-wide hang).
    if isinstance(data, dict):
        jid = data.get("job_id") or data.get("id")
        if jid:
            handler.send_header("X-Job-Id", str(jid))
    if os.environ.get("CORS") == "1":
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")
        handler.send_header("Access-Control-Expose-Headers", "X-Job-Id")
    handler.end_headers()
    handler.wfile.write(raw)


def api_error(code: str, message: str, detail: str = "", retryable: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "error": message,
        "error_detail": detail,
        "error_code": code,
        "error_info": {
            "code": code,
            "message": message,
            "detail": detail,
            "retryable": retryable,
        },
    }


def load_provider_config() -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        config = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
        if config.get("schema_version") != 1 or config.get("app") != "seedance":
            raise ValueError("providers.json schema_version/app mismatch")
        if not isinstance(config.get("providers"), dict) or not config["providers"]:
            raise ValueError("providers.json providers must be a non-empty object")
        return config, None
    except Exception as exc:
        return FALLBACK_PROVIDERS, {
            "code": "provider_config_error",
            "message": "供应商配置读取失败，请联系维护者",
            "detail": str(exc),
            "retryable": False,
        }


def provider_defaults(config: dict[str, Any], provider: str, model: str = "") -> dict[str, Any]:
    providers = config.get("providers") or {}
    provider_cfg = providers.get(provider) or providers.get(config.get("default_provider")) or next(iter(providers.values()))
    defaults = dict(provider_cfg.get("defaults") or {})
    if provider_cfg.get("base_url"):
        defaults.setdefault("base_url", provider_cfg["base_url"])
    defaults.setdefault("provider", provider)
    models = provider_cfg.get("models") if isinstance(provider_cfg.get("models"), list) else []
    selected = model or str(defaults.get("model") or "")
    for item in models:
        if isinstance(item, dict) and item.get("id") == selected:
            defaults.update(item.get("defaults") or {})
            break
    return defaults


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read_activity_log() -> list[dict[str, Any]]:
    if not ACTIVITY_PATH.exists():
        return []
    try:
        data = json.loads(ACTIVITY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_activity_log(items: list[dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    content = json.dumps(items[-ACTIVITY_LIMIT:], ensure_ascii=False, indent=2)
    _atomic_write(ACTIVITY_PATH, content)


def _filter_activity_by_ws(items: list[dict], ws_id: str) -> list[dict]:
    """Filter activity list to only show records for a workspace."""
    return [item for item in items if item.get("workspace_id") == ws_id]


def summarize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "api_key":
                result[key] = mask_key(str(item))
            elif key == "media" and isinstance(item, dict):
                result[key] = {
                    name: summarize_media_item(media_item)
                    for name, media_item in item.items()
                }
            else:
                result[key] = summarize_payload(item)
        return result
    if isinstance(value, list):
        return [summarize_payload(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return {"data_url": True, "chars": len(value)}
    return value


def summarize_media_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    result = {key: value for key, value in item.items() if key not in {"data_url"}}
    if item.get("data_url"):
        result["data_url"] = True
        result["chars"] = len(str(item["data_url"]))
    return result


def summarize_values_files(values: dict[str, Any], files: dict[str, tuple[str, bytes]]) -> dict[str, Any]:
    return {
        "values": {key: (mask_key(str(value)) if key == "api_key" else value) for key, value in values.items()},
        "files": {key: {"filename": item[0], "bytes": len(item[1])} for key, item in files.items()},
    }


def record_activity(record: dict[str, Any], ws_id: str = "localhost") -> None:
    items = read_activity_log()
    record.setdefault("id", uuid.uuid4().hex)
    record.setdefault("created_at", now_text())
    record.setdefault("updated_at", record["created_at"])
    record["workspace_id"] = ws_id
    items.append(record)
    write_activity_log(items)


def update_activity(activity_id: str | None, **updates: Any) -> None:
    if not activity_id:
        return
    items = read_activity_log()
    for item in items:
        if item.get("id") == activity_id:
            item.update(updates)
            item["updated_at"] = now_text()
            write_activity_log(items)
            return


def activity_list(ws_id: str = "localhost", show_all: bool = False, username: str = "") -> dict[str, Any]:
    items = read_activity_log()
    if not show_all:
        items = _filter_activity_by_ws(items, ws_id)
        if username:
            items = [it for it in items if it.get("username", "") == username]
    summary = []
    counts = {"total": len(items), "page": 0, "api": 0, "succeeded": 0, "failed": 0, "running": 0}
    for item in items:
        source = str(item.get("source") or "")
        status = str(item.get("status") or "")
        if source in counts:
            counts[source] += 1
        if status in counts:
            counts[status] += 1
        summary.append({
            "id": item.get("id"),
            "job_id": item.get("job_id"),
            "source": source,
            "status": status,
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "title": item.get("title"),
            "request_kind": item.get("request_kind"),
            "username": item.get("username", ""),
            "started_at": item.get("started_at"),
            "finished_at": item.get("finished_at"),
        })
    summary.reverse()
    return {"counts": counts, "records": summary}


def read_json_body(handler: SimpleHTTPRequestHandler, max_bytes: int = 200 * 1024 * 1024) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    if length > max_bytes:
        raise ValueError(f"JSON body too large: {length} bytes")
    raw = handler.rfile.read(length).decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def decode_data_url(data_url: str) -> tuple[str, bytes]:
    match = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", data_url, re.DOTALL)
    if not match:
        raise ValueError("Invalid data_url")
    mime = match.group(1) or "application/octet-stream"
    payload = urllib.parse.unquote_to_bytes(match.group(3))
    if match.group(2):
        payload = base64.b64decode(payload)
    return mime, payload


def filename_from_media(field: str, item: dict[str, Any], mime: str = "application/octet-stream") -> str:
    raw = str(item.get("filename") or "").strip()
    if raw:
        return Path(raw).name
    if item.get("url"):
        path = urllib.parse.urlparse(str(item["url"])).path
        name = Path(urllib.parse.unquote(path)).name
        if name:
            return name
    suffix = mimetypes.guess_extension(mime) or ".bin"
    return f"{field}{suffix}"


def media_item_to_file(field: str, item: Any) -> tuple[str, bytes] | None:
    if item in (None, "", False):
        return None
    if not isinstance(item, dict):
        raise ValueError(f"media.{field} must be an object")
    if item.get("data_url"):
        mime, blob = decode_data_url(str(item["data_url"]))
        return filename_from_media(field, item, mime), blob
    if item.get("url"):
        url = str(item["url"])
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                blob = resp.read()
                mime = resp.headers.get_content_type() or mimetypes.guess_type(url)[0] or "application/octet-stream"
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            raise RuntimeError(f"参考素材下载失败 (HTTP {exc.code}): {url} — {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"参考素材下载失败 (连接错误): {url} — {exc}") from exc
        if not blob:
            raise ValueError(f"media.{field} url returned empty content")
        return filename_from_media(field, item, mime), blob
    raise ValueError(f"media.{field} must include data_url or url")


def job_id_response(job_id: str) -> dict[str, Any]:
    return {"ok": True, "job_id": job_id, "status_url": f"/api/jobs/{job_id}"}


def read_preset(ws_id: str = "localhost") -> dict[str, Any]:
    path = _ws_preset_path(ws_id)
    if not path.exists():
        return {"values": {}, "media": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("values", {})
            data.setdefault("media", {})
            return data
    except Exception:
        pass
    return {"values": {}, "media": {}}


def preset_to_client(data: dict[str, Any], ws_id: str = "localhost") -> dict[str, Any]:
    media = {}
    media_dir = _ws_media_dir(ws_id)
    for field, item in data.get("media", {}).items():
        path = media_dir / item.get("stored", "")
        if path.exists():
            stored = path.name
            media[field] = {
                "filename": item.get("filename", path.name),
                "mime": item.get("mime", mimetypes.guess_type(path.name)[0] or "application/octet-stream"),
                "stored": stored,
                "url": f"/api/media/{urllib.parse.quote(stored)}?ws={ws_id}&v={int(path.stat().st_mtime)}",
            }
    return {"values": data.get("values", {}), "media": media}


def preset_for_client(ws_id: str = "localhost") -> dict[str, Any]:
    return preset_to_client(read_preset(ws_id), ws_id)


def copy_files_to_restore(values: dict[str, Any], files: dict[str, tuple[str, bytes]], prefix: str, ws_id: str = "localhost") -> dict[str, Any]:
    safe_values = {
        key: value for key, value in values.items()
        if key not in {"saved_media", "api_key", "api_key_override"}
    }
    media: dict[str, Any] = {}
    media_dir = _ws_media_dir(ws_id)
    media_dir.mkdir(parents=True, exist_ok=True)
    try:
        saved_media = json.loads(str(values.get("saved_media") or "{}"))
    except Exception:
        saved_media = {}
    if isinstance(saved_media, dict):
        for key, item in saved_media.items():
            if key not in FILE_FIELDS or not isinstance(item, dict):
                continue
            stored = Path(str(item.get("stored", ""))).name
            if stored and (media_dir / stored).exists():
                media[key] = {
                    "filename": item.get("filename", stored),
                    "stored": stored,
                    "mime": item.get("mime") or mimetypes.guess_type(stored)[0] or "application/octet-stream",
                }
    for key, file_data in files.items():
        if key not in FILE_FIELDS:
            continue
        filename, blob = file_data
        if not blob:
            continue
        suffix = Path(filename).suffix or mimetypes.guess_extension(mimetypes.guess_type(filename)[0] or "") or ".bin"
        stored = f"{prefix}_{uuid.uuid4().hex}_{key}{suffix}"
        (media_dir / stored).write_bytes(blob)
        media[key] = {
            "filename": filename,
            "stored": stored,
            "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        }
    return {"values": safe_values, "media": media}


def activity_record_for_client(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    data = json.loads(json.dumps(record))
    if isinstance(data.get("restore"), dict):
        data["restore"] = preset_to_client(data["restore"])
        return data
    request = data.get("request") or {}
    values = ((request.get("parsed") or {}).get("values") or request.get("values") or {})
    if isinstance(values, dict) and values:
        media: dict[str, Any] = {}
        try:
            saved_media = json.loads(str(values.get("saved_media") or "{}"))
        except Exception:
            saved_media = {}
        if isinstance(saved_media, dict):
            for key, item in saved_media.items():
                if key not in FILE_FIELDS or not isinstance(item, dict):
                    continue
                stored = Path(str(item.get("stored", ""))).name
                if stored and (media_dir / stored).exists():
                    media[key] = {
                        "filename": item.get("filename", stored),
                        "stored": stored,
                        "mime": item.get("mime") or mimetypes.guess_type(stored)[0] or "application/octet-stream",
                    }
        legacy = preset_to_client({"values": {
            key: value for key, value in values.items()
            if key not in {"api_key", "saved_media"} and key not in FILE_FIELDS
        }, "media": media})
        data["restore"] = {
            **legacy,
            "warning": "" if legacy.get("media") else "这条旧记录没有保存素材副本，只能恢复参数。",
        }
    return data


def safe_archive_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", (raw or "").strip()).strip("_")
    return name[:80] or time.strftime("seedance_%Y%m%d_%H%M%S")


def archive_path(name: str, ws_id: str = "localhost") -> Path:
    return _ws_dir(ws_id) / "archives" / f"{safe_archive_name(name)}.seedance"


def list_archives(handler: SimpleHTTPRequestHandler | None = None) -> list[dict[str, Any]]:
    ws = _workspace_id(handler) if handler else "localhost"
    dir_path = _ws_dir(ws) / "archives"
    dir_path.mkdir(parents=True, exist_ok=True)
    items = []
    # rglob so archives saved under future <user>/<date>/ subdirs are also
    # surfaced alongside legacy flat entries.
    candidates = [p for p in dir_path.rglob("*.seedance") if p.is_file()]
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        items.append(
            {
                "name": path.stem,
                "filename": path.name,
                "size": path.stat().st_size,
                "updated_at": int(path.stat().st_mtime),
            }
        )
    return items


def collect_media_from_form(form: cgi.FieldStorage, ws_id: str = "localhost") -> dict[str, Any]:
    preset = read_preset(ws_id)
    active_media = preset.get("media", {})
    saved_media = parse_saved_media(form)
    media = {}
    media_dir = _ws_media_dir(ws_id)
    for key, item in saved_media.items():
        if not isinstance(item, dict):
            continue
        stored = Path(str(item.get("stored", ""))).name
        if stored and (media_dir / stored).exists():
            media[key] = {
                "filename": item.get("filename", stored),
                "stored": stored,
                "mime": item.get("mime") or mimetypes.guess_type(stored)[0] or "application/octet-stream",
            }
        elif key in active_media:
            media[key] = active_media[key]
    media_dir.mkdir(parents=True, exist_ok=True)
    for key in FILE_FIELDS:
        file_data = get_file(form, key)
        if not file_data:
            continue
        filename, blob = file_data
        suffix = Path(filename).suffix or mimetypes.guess_extension(mimetypes.guess_type(filename)[0] or "") or ".bin"
        stored = f"{uuid.uuid4().hex}_{key}{suffix}"
        path = media_dir / stored
        path.write_bytes(blob)
        media[key] = {
            "filename": filename,
            "stored": stored,
            "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        }
    # Preserve existing media for any fields not explicitly set
    for key in FILE_FIELDS:
        if key not in media and key in active_media:
            media[key] = active_media[key]
    return media


def collect_workspace_snapshot_from_form(form: cgi.FieldStorage, ws_id: str = "localhost") -> dict[str, Any]:
    values = {key: get_field(form, key) for key in VALUE_FIELDS if key in form and not getattr(form[key], "filename", None)}
    return {"values": values, "media": collect_media_from_form(form, ws_id)}


def collect_preset_from_form(form: cgi.FieldStorage, ws_id: str = "localhost") -> dict[str, Any]:
    return collect_workspace_snapshot_from_form(form, ws_id)


def write_active_preset(preset: dict[str, Any], ws_id: str) -> None:
    ws_dir = _ws_preset_path(ws_id).parent
    ws_dir.mkdir(parents=True, exist_ok=True)
    _ws_media_dir(ws_id).mkdir(parents=True, exist_ok=True)
    content = json.dumps(preset, ensure_ascii=False, indent=2)
    _atomic_write(_ws_preset_path(ws_id), content)


def save_archive_file(name: str, preset: dict[str, Any], ws_id: str = "localhost", handler: SimpleHTTPRequestHandler | None = None) -> Path:
    path = archive_path(name, ws_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Strip secrets before persisting
    safe_preset = dict(preset)
    safe_preset["values"] = {k: v for k, v in safe_preset.get("values", {}).items()
                             if k not in ("api_key", "api_key_override")}
    ws_media = _ws_media_dir(ws_id)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("preset.json", json.dumps(safe_preset, ensure_ascii=False, indent=2))
        for field, item in preset.get("media", {}).items():
            stored = item.get("stored", "")
            src = ws_media / stored
            if src.exists():
                zf.write(src, f"media/{stored}")
    return path


def load_archive_file(name: str, handler: SimpleHTTPRequestHandler | None = None) -> dict[str, Any]:
    ws = _workspace_id(handler) if handler else "localhost"
    path = archive_path(name, ws)
    migrated = False
    if not path.exists():
        # Legacy fallback — try top-level, then IP-scoped
        for legacy in [ARCHIVE_DIR / f"{safe_archive_name(name)}.seedance"]:
            if legacy.exists():
                path = legacy
                migrated = True
                break
        # Also check old IP directories
        if not path.exists() and ARCHIVE_DIR.exists():
            for ip_dir in sorted(ARCHIVE_DIR.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
                if ip_dir.is_dir():
                    p = ip_dir / f"{safe_archive_name(name)}.seedance"
                    if p.exists():
                        path = p
                        migrated = True
                        break
        if not path.exists():
            raise FileNotFoundError(f"Archive not found: {name}")
    ws_media = _ws_media_dir(ws)
    ws_media.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "r") as zf:
        preset = json.loads(zf.read("preset.json").decode("utf-8"))
        names = set(zf.namelist())
        for item in preset.get("media", {}).values():
            original = Path(str(item.get("stored", ""))).name
            archive_name = f"media/{original}"
            if archive_name not in names:
                continue
            target_name = f"{uuid.uuid4().hex}_{original}"
            target = ws_media / target_name
            target.write_bytes(zf.read(archive_name))
            item["stored"] = target_name
    write_active_preset(preset, ws)
    if migrated and handler is not None:
        save_archive_file(name, preset, ws)
    return preset_for_client(ws)


def choose_output_dir() -> str:
    prompt = "选择 Seedance 输出目录"
    if sys.platform == "darwin":
        script = f'POSIX path of (choose folder with prompt "{prompt}")'
        result = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=60)
        return result.stdout.strip().rstrip("/")
    if sys.platform.startswith("win"):
        ps = (
            "$folder = (New-Object -ComObject Shell.Application)."
            f"BrowseForFolder(0, '{prompt}', 0, 0); "
            "if ($folder) { [Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8; "
            "Write-Output $folder.Self.Path }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            check=True,
            capture_output=True,
            text=True,
        )
        selected = result.stdout.strip()
        if selected:
            return selected
        raise RuntimeError("未选择输出目录")

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askdirectory(title=prompt)
    finally:
        root.destroy()
    if selected:
        return selected
    raise RuntimeError("未选择输出目录")


def optimize_prompt(user_prompt: str) -> dict[str, Any]:
    """Optimize a user prompt using DeepSeek with the Seedance 2.0 skill."""
    if not SEEDANCE_SKILL:
        return {"ok": False, "error": "SKILL.md 未找到或为空，无法进行优化"}
    if not user_prompt.strip():
        return {"ok": False, "error": "请先输入提示词"}
    api_key = _load_deepseek_key()
    if not api_key:
        return {"ok": False, "error": (
            "提示词优化未配置 DeepSeek API Key。"
            f"请将 sk-... 写入 {DEEPSEEK_KEY_PATH}（一行,不带引号）"
        )}

    body = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SEEDANCE_SKILL},
            {"role": "user", "content": user_prompt.strip()},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    try:
        result = request_json(
            "POST",
            "https://api.deepseek.com/v1/chat/completions",
            api_key,
            body,
            timeout=120,
        )
        optimized = (result.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not optimized:
            return {"ok": False, "error": "DeepSeek 返回为空，请稍后重试"}
        return {"ok": True, "optimized": optimized.strip()}
    except RuntimeError as exc:
        msg = str(exc)
        if "401" in msg:
            msg = f"DeepSeek API Key 无效或已过期({msg[:120]})"
        return {"ok": False, "error": f"优化请求失败：{msg}"}


def mask_key(key: str) -> str:
    if not key:
        return ""
    return f"{key[:5]}...{key[-4:]}" if len(key) > 12 else "***"


def normalize_status(value: str | None) -> str:
    status = (value or "").lower()
    if status == "success":
        return "succeeded"
    if status in {"fail", "failure"}:
        return "failed"
    return status


def request_json(method: str, url: str, api_key: str, body: dict[str, Any] | None = None, timeout: int = 600) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {raw}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"API 请求超时或连接失败 ({exc.__class__.__name__}: {exc})") from exc


def _public_base_url(handler: SimpleHTTPRequestHandler) -> str | None:
    """Build the public scheme://host prefix from forwarded headers set by the portal proxy.
    Returns None when no X-Forwarded-Host is present (subapp accessed directly on LAN/loopback
    where there's no public reverse-proxied URL to advertise)."""
    host = (handler.headers.get("X-Forwarded-Host") or "").strip()
    if not host:
        return None
    proto = (handler.headers.get("X-Forwarded-Proto") or "https").strip()
    return f"{proto}://{host}"


def register_refmedia(blob: bytes, mime: str, filename: str) -> tuple[str, str]:
    """Stash a reference media blob under state/refmedia/ and return (token, ext).
    The blob is exposed at GET /api/refmedia/<token><ext> for REFMEDIA_TTL seconds."""
    token = uuid.uuid4().hex
    ext = Path(filename).suffix or mimetypes.guess_extension(mime) or ".bin"
    REFMEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = REFMEDIA_DIR / f"{token}{ext}"
    path.write_bytes(blob)
    with REFMEDIA_LOCK:
        REFMEDIA[token] = {"path": path, "mime": mime, "ext": ext,
                           "expires_at": time.time() + REFMEDIA_TTL}
    return token, ext


def cleanup_refmedia_loop() -> None:
    """Background sweep — every 10 minutes, evict any refmedia entries past expires_at."""
    while True:
        time.sleep(600)
        now = time.time()
        to_remove: list[tuple[str, Path]] = []
        with REFMEDIA_LOCK:
            for tk, m in list(REFMEDIA.items()):
                if m.get("expires_at", 0) < now:
                    to_remove.append((tk, m["path"]))
                    del REFMEDIA[tk]
        for _tk, path in to_remove:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def reset_refmedia_dir() -> None:
    """Clear the refmedia directory on startup. Tokens are in-memory only,
    so any leftover files from a previous process are unreachable anyway."""
    try:
        if REFMEDIA_DIR.exists():
            for child in REFMEDIA_DIR.iterdir():
                if child.is_file():
                    try:
                        child.unlink(missing_ok=True)
                    except Exception:
                        pass
    except Exception:
        pass


def upload_file(upload_url: str, api_key: str, blob: bytes, filename: str, content_type: str, extra_fields: dict[str, str] | None = None) -> dict[str, str]:
    """Upload a file and return the reference dict for use in content items.
    Returns {"url": "..."} for t8star, {"file_id": "..."} for volcengine Ark."""
    boundary = f"----seedance{uuid.uuid4().hex}"
    body = bytearray()
    for k, v in (extra_fields or {}).items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        body.extend(str(v).encode())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        (
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    body.extend(blob)
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        upload_url,
        data=bytes(body),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        raise RuntimeError(f"文件上传失败 (HTTP {exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"文件上传失败 (连接错误): {exc}") from exc
    if data.get("url"):
        return {"url": str(data["url"])}
    if data.get("id"):
        return {"file_id": str(data["id"])}
    raise RuntimeError(f"Upload did not return url or id: {data}")


def media_reference(provider: str, base_url: str, api_key: str, blob: bytes, filename: str, mime: str, request_host: str | None = None) -> dict[str, str]:
    # Ark generation tasks (/contents/generations/tasks) handle reference media in two
    # distinct ways depending on the slot:
    #
    #   image_url.url      — accepts data: URL or https://. Use base64 data URL (zero
    #                        round-trip, no public exposure of the image).
    #   video_url.url /
    #   audio_url.url      — accepts ONLY https://, and Ark issues an
    #                        unauthenticated GET to that URL. The /files/{id}/content
    #                        endpoint needs Authorization so it doesn't work either.
    #
    # Solution: PUT video/audio into a public-read TOS bucket (random hex object key)
    # and return the bucket's https URL. The bucket must be in the same volcengine
    # account as Ark and configured public-read; AK/SK is inherited from the portrait
    # sub-app's config via portal-injected env (TOS_ACCESS_KEY/TOS_SECRET_KEY).
    #
    # Do NOT change the image branch to use /files upload — the file_id cannot be
    # referenced from image_url.url (see seedance/docs/volcengine-media.md).
    # Ark generation tasks expect every reference media URL to be public https.
    # The simplest, most uniform path is: PUT every image/video/audio blob into
    # the company TOS bucket (public-read) and return the resulting URL. This
    # replaces the legacy split (image as data:base64 URL, video/audio via local
    # refmedia pool through a public-facing host).
    # Provider is locked to volcengine; the t8star path was removed. Anything
    # else (e.g. a stale curl call passing provider=t8star) is rejected here.
    if provider != "volcengine":
        raise RuntimeError(f"only volcengine provider is supported (got {provider!r})")
    public_url = tos_upload(blob, mime, filename)
    return {"url": public_url}



def extract_video_url(data: dict[str, Any]) -> str | None:
    content = data.get("content")
    if isinstance(content, dict):
        url = content.get("video_url") or content.get("videoUrl")
        if url:
            return str(url)
    nested = data.get("data")
    if isinstance(nested, dict):
        url = extract_video_url(nested)
        if url:
            return url
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "video_url":
                value = item.get("video_url")
                if isinstance(value, dict) and value.get("url"):
                    return str(value["url"])
                if isinstance(value, str):
                    return value
    for key in ("video_url", "videoUrl", "output"):
        if data.get(key):
            return str(data[key])
    results = data.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
    return None


def download_video(url: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            out_path.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        raise RuntimeError(f"视频下载失败 (HTTP {exc.code}): {url[:120]} — {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"视频下载失败 (连接错误): {url[:120]} — {exc}") from exc


def set_job(job_id: str, **updates: Any) -> None:
    with JOBS_LOCK:
        JOBS[job_id].update(updates)


def add_event(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        JOBS[job_id].setdefault("events", []).append({"time": time.strftime("%H:%M:%S"), "message": message})


def parse_bool(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def get_field(form: cgi.FieldStorage, name: str, default: str = "") -> str:
    item = form[name] if name in form else None
    if item is None or item.filename:
        return default
    return str(item.value)


def get_file(form: cgi.FieldStorage, name: str) -> tuple[str, bytes] | None:
    item = form[name] if name in form else None
    if item is None or not item.filename:
        return None
    blob = item.file.read()
    if not blob:
        return None
    return Path(item.filename).name, blob


def parse_saved_media(form: cgi.FieldStorage) -> dict[str, Any]:
    raw = get_field(form, "saved_media", "{}")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_file_or_saved(form: cgi.FieldStorage, name: str, ws_id: str = "localhost") -> tuple[str, bytes] | None:
    uploaded = get_file(form, name)
    if uploaded:
        return uploaded
    saved = parse_saved_media(form).get(name)
    if not isinstance(saved, dict):
        return None
    stored = Path(str(saved.get("stored", ""))).name
    if stored:
        path = _ws_media_dir(ws_id) / stored
        if path.exists():
            return saved.get("filename", path.name), path.read_bytes()
    preset = read_preset(ws_id)
    item = preset.get("media", {}).get(name)
    if not item:
        return None
    path = _ws_media_dir(ws_id) / item.get("stored", "")
    if not path.exists():
        return None
    return item.get("filename", path.name), path.read_bytes()


def replace_refs(prompt: str) -> str:
    return re.sub(r"@ref_image(\d+)", r"Image \1", prompt)


def build_payload(form: cgi.FieldStorage, api_key: str, base_url: str, run_index: int, ws_id: str = "localhost", request_host: str | None = None) -> dict[str, Any]:
    provider = get_field(form, "provider", "volcengine")
    prompt = replace_refs(get_field(form, "prompt").strip())
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    has_frame_input = False
    has_reference_input = False
    has_visual_reference = False

    first = get_file_or_saved(form, "first_frame", ws_id)
    if first:
        has_frame_input = True
        filename, blob = first
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        ref = media_reference(provider, base_url, api_key, blob, filename, mime, request_host=request_host)
        content.append({"type": "image_url", "image_url": ref, "role": "first_frame"})

    last = get_file_or_saved(form, "last_frame", ws_id)
    if last:
        has_frame_input = True
        filename, blob = last
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        ref = media_reference(provider, base_url, api_key, blob, filename, mime, request_host=request_host)
        content.append({"type": "image_url", "image_url": ref, "role": "last_frame"})

    for i in range(1, 10):
        file_data = get_file_or_saved(form, f"ref_image_{i}", ws_id)
        if not file_data:
            continue
        has_reference_input = True
        has_visual_reference = True
        filename, blob = file_data
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        ref = media_reference(provider, base_url, api_key, blob, filename, mime, request_host=request_host)
        content.append({"type": "image_url", "image_url": ref, "role": "reference_image"})

    for i in range(1, 4):
        file_data = get_file_or_saved(form, f"ref_video_{i}", ws_id)
        if not file_data:
            continue
        has_reference_input = True
        has_visual_reference = True
        filename, blob = file_data
        mime = mimetypes.guess_type(filename)[0] or "video/mp4"
        ref = media_reference(provider, base_url, api_key, blob, filename, mime, request_host=request_host)
        content.append({"type": "video_url", "video_url": ref, "role": "reference_video"})

    for i in range(1, 4):
        file_data = get_file_or_saved(form, f"ref_audio_{i}", ws_id)
        if not file_data:
            continue
        has_reference_input = True
        filename, blob = file_data
        mime = mimetypes.guess_type(filename)[0] or "audio/wav"
        ref = media_reference(provider, base_url, api_key, blob, filename, mime, request_host=request_host)
        content.append({"type": "audio_url", "audio_url": ref, "role": "reference_audio"})

    seed_raw = get_field(form, "seed", "").strip()
    seed = int(seed_raw) if seed_raw else None
    if seed is not None and parse_bool(get_field(form, "vary_seed")):
        seed += run_index

    payload: dict[str, Any] = {
        "model": get_field(form, "custom_model").strip() or get_field(form, "model", "doubao-seedance-2-0-260128"),
        "content": content,
        "duration": int(get_field(form, "duration", "12")),
        "ratio": get_field(form, "ratio", "16:9"),
        "resolution": get_field(form, "resolution", "720p"),
        "generate_audio": parse_bool(get_field(form, "generate_audio")),
        "watermark": parse_bool(get_field(form, "watermark")),
    }
    if seed is not None:
        payload["seed"] = seed
    if parse_bool(get_field(form, "return_last_frame")):
        payload["return_last_frame"] = True
    if parse_bool(get_field(form, "web_search")):
        payload["tools"] = [{"type": "web_search"}]
    if provider == "volcengine":
        if has_frame_input and has_reference_input:
            raise RuntimeError("豆包官方 API 不允许首尾帧与参考图/视频/音频混用，请二选一提交。")
        if has_reference_input and not has_visual_reference:
            raise RuntimeError("豆包官方 API 不允许单独输入参考音频，请至少加入 1 个参考图或参考视频。")
    return payload


def content_counts(payload: dict[str, Any]) -> str:
    counts = {"text": 0, "image_url": 0, "video_url": 0, "audio_url": 0}
    for item in payload.get("content", []):
        if isinstance(item, dict):
            key = item.get("type")
            if key in counts:
                counts[key] += 1
    return ", ".join(f"{k}:{v}" for k, v in counts.items())


def resolve_output_dir(raw: str | None) -> Path:
    if raw and raw.strip():
        path = Path(raw.strip()).expanduser()
    else:
        path = OUTPUT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def desktop_output_dir() -> str:
    desktop = Path.home() / "Desktop"
    parent = desktop if desktop.exists() else Path.home()
    return str((parent / "seedance_outputs").resolve())


def open_output_dir(raw: str | None) -> str:
    path = resolve_output_dir(raw)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])
    return str(path)


def referenced_media_names() -> set[str]:
    names: set[str] = set()

    def collect_media(media: Any) -> None:
        if not isinstance(media, dict):
            return
        for item in media.values():
            if not isinstance(item, dict):
                continue
            stored = Path(str(item.get("stored", ""))).name
            if stored:
                names.add(stored)

    def collect_saved_media(values: Any) -> None:
        if not isinstance(values, dict):
            return
        try:
            saved = json.loads(str(values.get("saved_media") or "{}"))
        except Exception:
            saved = {}
        collect_media(saved)

    collect_media(read_preset().get("media"))
    for record in read_activity_log():
        collect_media((record.get("restore") or {}).get("media"))
        request = record.get("request") or {}
        collect_saved_media(request.get("values"))
        collect_saved_media((request.get("parsed") or {}).get("values"))
    return names


def cleanup_cache(media_days: int = 30, log_days: int = 14) -> dict[str, Any]:
    now = time.time()
    referenced = referenced_media_names()
    media_cutoff = now - max(1, media_days) * 86400
    log_cutoff = now - max(1, log_days) * 86400
    stats = {
        "ok": True,
        "media_days": media_days,
        "log_days": log_days,
        "media_deleted": 0,
        "logs_deleted": 0,
        "bytes_deleted": 0,
        "kept_referenced_media": len(referenced),
    }
    if MEDIA_DIR.exists():
        for path in MEDIA_DIR.iterdir():
            if not path.is_file() or path.name in referenced or path.stat().st_mtime >= media_cutoff:
                continue
            size = path.stat().st_size
            path.unlink()
            stats["media_deleted"] += 1
            stats["bytes_deleted"] += size
    logs_dir = ROOT / "logs"
    if logs_dir.exists():
        for path in logs_dir.iterdir():
            if not path.is_file() or path.stat().st_mtime >= log_cutoff:
                continue
            size = path.stat().st_size
            path.unlink()
            stats["logs_deleted"] += 1
            stats["bytes_deleted"] += size
    return stats


def api_schema() -> dict[str, Any]:
    config, config_error = load_provider_config()
    return {
        "app": "seedance",
        "endpoints": {
            "schema": "GET /api/schema",
            "submit_json": "POST /api/jobs/json",
            "job_status": "GET /api/jobs/{job_id}",
            "download": "GET /api/download/{token}",
        },
        "providers": config.get("providers", {}),
        "default_provider": config.get("default_provider"),
        "config_error": config_error,
        "value_fields": sorted(VALUE_FIELDS),
        "file_fields": sorted(FILE_FIELDS),
        "media_item": {
            "data_url": "data:image/png;base64,...",
            "url": "https://example.com/file.png",
            "filename": "optional-name.png",
        },
        "example": {
            "provider": "volcengine",
            "model": "doubao-seedance-2-0-260128",
            "prompt": "视频提示词",
            "duration": 8,
            "ratio": "16:9",
            "resolution": "720p",
            "repeat_count": 1,
            "concurrency": 1,
            "media": {
                "ref_image_1": {"data_url": "data:image/png;base64,...", "filename": "ref.png"},
                "ref_video_1": {"url": "https://example.com/ref.mp4"},
                "ref_audio_1": {"url": "https://example.com/ref.mp3"},
            },
        },
    }


def request_template() -> dict[str, Any]:
    config, config_error = load_provider_config()
    provider = str(config.get("default_provider") or "volcengine")
    defaults = provider_defaults(config, provider)
    minimal = {
        "api_key": "YOUR_API_KEY",
        "prompt": "describe the video you want",
        "media": {
            "ref_image_1": {
                "filename": "reference.png",
                "data_url": "data:image/png;base64,..."
            }
        },
    }
    media = {
        "first_frame": None,
        "last_frame": None,
        **{f"ref_image_{i}": None for i in range(1, 10)},
        **{f"ref_video_{i}": None for i in range(1, 4)},
        **{f"ref_audio_{i}": None for i in range(1, 4)},
    }
    media["ref_image_1"] = {"filename": "reference.png", "data_url": "data:image/png;base64,..."}
    full = {
        "api_key": "YOUR_API_KEY",
        "provider": provider,
        "base_url": defaults.get("base_url", OFFICIAL_ARK_BASE_URL),
        "model": defaults.get("model", "doubao-seedance-2-0-260128"),
        "custom_model": "",
        "prompt": "describe the video you want",
        "duration": defaults.get("duration", 12),
        "resolution": defaults.get("resolution", "720p"),
        "ratio": defaults.get("ratio", "16:9"),
        "seed": "",
        "vary_seed": defaults.get("vary_seed", True),
        "generate_audio": False,
        "watermark": False,
        "return_last_frame": False,
        "web_search": False,
        "repeat_count": defaults.get("repeat_count", 1),
        "concurrency": defaults.get("concurrency", 1),
        "poll_interval": defaults.get("poll_interval", 10),
        "timeout": defaults.get("timeout", 3600),
        "media": media,
    }
    return {
        "ok": config_error is None,
        "app": "seedance",
        "endpoint": "POST /api/jobs/json",
        "content_type": "application/json",
        "config_error": config_error,
        "templates": {"minimal": minimal, "full": full},
        "field_notes": {
            "api_key": "可省略；省略时使用本地配置中的 key。",
            "custom_model": "高级字段；非空时覆盖 model 下拉值。",
            "media.*.data_url": "使用 data URL，例如 data:video/mp4;base64,...。",
            "first_frame/last_frame": "首尾帧槽位；豆包官方 API 不允许与参考素材混用。",
            "repeat_count": "请求生成次数；如果 concurrency 更大，后端会按 concurrency 数启动。",
            "concurrency": "同一任务内同时运行的生成数量。",
        },
    }


def values_files_from_json(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[str, bytes]]]:
    config, config_error = load_provider_config()
    if config_error:
        raise ValueError(f"{config_error['message']}: {config_error['detail']}")
    incoming = {key: payload[key] for key in VALUE_FIELDS if key in payload and payload[key] is not None}
    provider = str(incoming.get("provider") or config.get("default_provider") or "volcengine")
    values = provider_defaults(config, provider, str(incoming.get("model") or ""))
    values.update(incoming)
    values["provider"] = provider
    if values.get("custom_model"):
        values["model"] = str(values["custom_model"]).strip()
    values.setdefault("prompt", "")

    media = payload.get("media") or {}
    if not isinstance(media, dict):
        raise ValueError("media must be an object")
    files: dict[str, tuple[str, bytes]] = {}
    for field in FILE_FIELDS:
        if field not in media:
            continue
        file_data = media_item_to_file(field, media[field])
        if file_data:
            files[field] = file_data
    return values, files


def create_job(values: dict[str, Any], files: dict[str, tuple[str, bytes]], source: str, request_kind: str, request_data: dict[str, Any], ws_id: str = "localhost", username: str = "") -> str:
    job_id = uuid.uuid4().hex
    activity_id = uuid.uuid4().hex
    try:
        per_item_duration = int(values.get("duration") or 0)
    except (TypeError, ValueError):
        per_item_duration = 0
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id, "status": "queued", "events": [], "results": [], "errors": [],
            "done": 0, "total": 0, "duration": per_item_duration,
            "username": username,
            "submitted_at": time.time(),
            "started_at": None,
            "finished_at": None,
        }
    response = job_id_response(job_id)
    record_activity({
        "id": activity_id,
        "job_id": job_id,
        "source": source,
        "request_kind": request_kind,
        "status": "running",
        "title": str(values.get("prompt") or "")[:80] or "Seedance task",
        "request": request_data,
        "response": response,
        "workspace_id": ws_id,
        "username": username,
        "started_at": time.time(),
        "restore": copy_files_to_restore(values, files, activity_id, ws_id),
    })
    thread = threading.Thread(target=run_job, args=(job_id, values, files, activity_id, ws_id), daemon=True)
    thread.start()
    return job_id


def run_one(job_id: str, index: int, form_values: dict[str, Any], form_files: dict[str, tuple[str, bytes]], ws_id: str = "localhost") -> dict[str, Any]:
    class MemoryForm(dict):
        pass

    form = MemoryForm()
    for key, value in form_values.items():
        form[key] = type("Field", (), {"value": value, "filename": None})()
    for key, (filename, blob) in form_files.items():
        form[key] = type("Field", (), {"filename": filename, "file": type("Reader", (), {"read": lambda self, b=blob: b})()})()

    api_key = str(form_values["api_key"]).strip()
    provider = str(form_values.get("provider") or "volcengine")
    # Provider is hardcoded to volcengine — t8star path removed.
    base_url = str(form_values.get("base_url") or OFFICIAL_ARK_BASE_URL).rstrip("/")
    create_url = f"{base_url}/contents/generations/tasks"
    add_event(job_id, f"Run {index}: preparing payload")
    request_host = form_values.get("_request_host") or None
    payload = build_payload(form, api_key, base_url, index - 1, ws_id, request_host=request_host)
    add_event(job_id, f"Run {index}: creating task ({content_counts(payload)})")
    create_result = request_json("POST", create_url, api_key, payload)
    task_id = create_result.get("id") or create_result.get("task_id")
    if not task_id:
        raise RuntimeError(f"No task id returned: {create_result}")
    add_event(job_id, f"Run {index}: task {task_id}")

    status_url = f"{create_url}/{task_id}"
    start = time.time()
    timeout = int(form_values.get("timeout") or 3600)
    poll_interval = int(form_values.get("poll_interval") or 10)
    while True:
        if time.time() - start > timeout:
            raise RuntimeError(f"Task {task_id} timed out after {timeout}s")
        time.sleep(poll_interval)
        status_result = request_json("GET", status_url, api_key, timeout=60)
        status = normalize_status(status_result.get("status"))
        add_event(job_id, f"Run {index}: {status or 'unknown'}")
        if status not in TERMINAL_STATUSES:
            continue
        if status != "succeeded":
            raise RuntimeError(f"Task {task_id} ended as {status}: {status_result}")
        video_url = extract_video_url(status_result)
        if not video_url:
            raise RuntimeError(f"Task {task_id} succeeded but no video URL was found")
        raw_output_dir = form_values.get("output_dir")
        if not raw_output_dir:
            with JOBS_LOCK:
                username = JOBS.get(job_id, {}).get("username", "")
            form_values["output_dir"] = str(_user_day_subdir(OUTPUT_DIR, username))
        out_dir = resolve_output_dir(form_values.get("output_dir"))
        custom_name = form_values.get("output_name", "").strip()
        if custom_name:
            total = max(1, int(form_values.get("repeat_count") or 1), int(form_values.get("concurrency") or 1))
            if total > 1:
                out_name = f"{custom_name}-{index}.mp4"
            else:
                out_name = f"{custom_name}.mp4"
            if (out_dir / out_name).exists():
                out_name = f"{custom_name}-{index}_{time.strftime('%H%M%S')}.mp4"
        else:
            out_name = f"{time.strftime('%Y%m%d_%H%M%S')}_run{index}_{task_id}.mp4"
        out_path = out_dir / out_name
        download_video(video_url, out_path)
        file_token = uuid.uuid4().hex
        with JOBS_LOCK:
            FILES[file_token] = out_path
        save_files_map()
        add_event(job_id, f"Run {index}: downloaded {out_name}")
        return {
            "index": index,
            "task_id": task_id,
            "status": "succeeded",
            "video_url": video_url,
            "download_url": f"/api/download/{file_token}",
            "filename": out_name,
            "local_path": str(out_path),
        }


def run_job(job_id: str, form_values: dict[str, Any], form_files: dict[str, tuple[str, bytes]], activity_id: str | None = None, ws_id: str = "localhost") -> None:
    try:
        requested_count = max(1, min(20, int(form_values.get("repeat_count") or 1)))
        requested_concurrency = max(1, min(20, int(form_values.get("concurrency") or 1)))
        count = max(requested_count, requested_concurrency)
        concurrency = min(count, requested_concurrency)
        set_job(job_id, status="running", total=count, done=0, results=[], errors=[], started_at=time.time())
        add_event(job_id, f"Started {count} run(s), concurrency {concurrency}, key {mask_key(form_values.get('api_key', ''))}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(run_one, job_id, i, form_values, form_files, ws_id) for i in range(1, count + 1)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    with JOBS_LOCK:
                        JOBS[job_id]["results"].append(result)
                        JOBS[job_id]["done"] += 1
                except Exception as exc:
                    with JOBS_LOCK:
                        JOBS[job_id]["errors"].append(str(exc))
                        JOBS[job_id]["done"] += 1
                    add_event(job_id, f"Error: {exc}")
        with JOBS_LOCK:
            errors = JOBS[job_id]["errors"]
            final_job = json.loads(json.dumps(JOBS[job_id]))
        final_status = "failed" if errors else "succeeded"
        set_job(job_id, status=final_status, finished_at=time.time())
        final_job["status"] = final_status
        update_activity(activity_id, status=final_status, result=final_job, finished_at=time.time())
        add_event(job_id, "Finished")
        report_final_to_portal(job_id, final_status)
    except Exception as exc:
        set_job(job_id, status="failed", errors=[str(exc)], finished_at=time.time())
        with JOBS_LOCK:
            final_job = json.loads(json.dumps(JOBS.get(job_id, {})))
        update_activity(activity_id, status="failed", error=str(exc), result=final_job, finished_at=time.time())
        add_event(job_id, f"Fatal: {exc}")
        report_final_to_portal(job_id, "failed")


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Injected globally so every response carries nosniff without touching
        # each send_header site. Prevents browsers from executing an uploaded
        # .jpg whose bytes are actually HTML/SVG+JS.
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _reject_oversized_upload(self) -> bool:
        raw = self.headers.get("Content-Length")
        if not raw:
            return False
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return False
        if n > MAX_UPLOAD_BYTES:
            body = json.dumps({
                "ok": False,
                "error": f"upload too large: {n} bytes (limit {MAX_UPLOAD_BYTES})",
            }).encode("utf-8")
            self.send_response(413)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return True
        return False

    def translate_path(self, path: str) -> str:
        path = urllib.parse.urlparse(path).path
        if path.startswith("/outputs/"):
            return _safe_join_or_root(OUTPUT_DIR, path.removeprefix("/outputs/"))
        if path in {"/", "/index.html"}:
            return str(STATIC_DIR / "index.html")
        return _safe_join_or_root(STATIC_DIR, path.lstrip("/"))

    def do_GET(self) -> None:
        self._raw_path = self.path
        self.path = urllib.parse.urlparse(self.path).path
        if self.path == "/api/v1/meta":
            json_response(self, 200, {
                "app": "seedance",
                "version": "1.0.0",
                "port": int(os.environ.get("PORT", "8787")),
                "capabilities": ["text2video", "image2video", "frames2video", "multimodal2video"],
                "status": "ready",
            })
            return
        if self.path == "/api/config":
            providers, config_error = load_provider_config()
            json_response(self, 200, {
                "ok": config_error is None,
                "providers": providers.get("providers", {}),
                "default_provider": providers.get("default_provider"),
                "config_error": config_error,
            })
            return
        if self.path == "/api/request-template":
            json_response(self, 200, request_template())
            return
        if self.path == "/api/preset":
            json_response(self, 200, preset_for_client(_workspace_id(self)))
            return
        if self.path == "/api/archives":
            json_response(self, 200, {"archives": list_archives(self)})
            return
        if self.path == "/api/schema":
            json_response(self, 200, api_schema())
            return
        if self.path == "/api/jobs":
            sees_all, username = _view_scope(self)
            items = []
            with JOBS_LOCK:
                for jid, j in JOBS.items():
                    if not sees_all and j.get("username", "") != username:
                        continue
                    results = []
                    for r in (j.get("results") or []):
                        results.append({
                            "download_url": r.get("download_url", ""),
                            "filename": r.get("filename", ""),
                            "index": r.get("index", ""),
                            "task_id": r.get("task_id", ""),
                        })
                    items.append({
                        "job_id": jid,
                        "status": j.get("status", "pending"),
                        "model": j.get("model", ""),
                        "prompt": (j.get("params", {}).get("prompt") or j.get("prompt", ""))[:200],
                        "created_at": j.get("created_at", ""),
                        "submitted_at": j.get("submitted_at"),
                        "started_at": j.get("started_at"),
                        "finished_at": j.get("finished_at"),
                        "username": j.get("username", ""),
                        "results": results,
                        "errors": j.get("errors", []),
                        "done": j.get("done", 0),
                        "workspace_id": j.get("workspace_id", ""),
                        "total": j.get("total", 0),
                    })
            items.sort(key=lambda it: (it.get("submitted_at") or 0), reverse=True)
            json_response(self, 200, {"ok": True, "jobs": items})
            return
        if self.path == "/api/activity":
            sees_all, username = _view_scope(self)
            json_response(self, 200, activity_list(show_all=sees_all, username=username))
            return
        if self.path.startswith("/api/activity/"):
            activity_id = self.path.rsplit("/", 1)[-1]
            ws = _workspace_id(self)
            record = next((item for item in read_activity_log() if item.get("id") == activity_id), None)
            if record and record.get("workspace_id") != ws and not _is_admin(self):
                record = None
            json_response(self, 200 if record else 404, activity_record_for_client(record) or {"error": "activity not found"})
            return
        if self.path == "/api/default-output-dir":
            json_response(self, 200, {"path": desktop_output_dir()})
            return
        if self.path.startswith("/api/preset-media/"):
            field = self.path.rsplit("/", 1)[-1]
            ws = _workspace_id(self)
            preset = read_preset(ws)
            item = preset.get("media", {}).get(field)
            # Collapse stored to bare basename — preset.json is normally written
            # by our own upload path but treat it as untrusted anyway.
            stored_name = Path(item.get("stored", "")).name if item else ""
            path = _ws_media_dir(ws) / stored_name if stored_name else None
            if not item or not path or not path.exists():
                json_response(self, 404, {"error": "media not found"})
                return
            mime = item.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            try:
                self.wfile.write(path.read_bytes())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if urllib.parse.urlparse(self.path).path.startswith("/api/media/"):
            raw_name = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
            stored = Path(urllib.parse.unquote(raw_name)).name
            ws = _workspace_id(self)
            path = _ws_media_dir(ws) / stored
            if not path.exists():
                json_response(self, 404, {"error": "media not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            try:
                self.wfile.write(path.read_bytes())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if self.path.startswith("/api/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                data = json.loads(json.dumps(job)) if job else None
            json_response(self, 200 if data else 404, data or {"error": "job not found"})
            return
        if self.path.startswith("/api/refmedia/"):
            # Anonymous endpoint for Ark to fetch reference media. Token is an
            # unguessable hex UUID; entries expire after REFMEDIA_TTL seconds.
            # Strip query string and extension to recover the token.
            raw = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
            token = raw.split(".", 1)[0]
            with REFMEDIA_LOCK:
                meta = REFMEDIA.get(token)
            if not meta or meta.get("expires_at", 0) < time.time() or not meta["path"].exists():
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path = meta["path"]
            self.send_response(200)
            self.send_header("Content-Type", meta.get("mime") or "application/octet-stream")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Cache-Control", "public, max-age=3600")
            # Public endpoint — leave CORS open so Ark's fetcher (no Origin) is fine
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(path.read_bytes())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if self.path.startswith("/api/download/"):
            token = self.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                path = FILES.get(token)
            if not path or not path.exists():
                json_response(self, 404, {"error": "file not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            try:
                self.wfile.write(path.read_bytes())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        super().do_GET()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        if os.environ.get("CORS") == "1":
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self._reject_oversized_upload():
            return
        self._raw_path = self.path
        self.path = urllib.parse.urlparse(self.path).path
        if self.path == "/api/choose-output-dir":
            client_ip = self.headers.get("X-Forwarded-For") or self.client_address[0]
            if client_ip not in ("127.0.0.1", "::1", "localhost"):
                json_response(self, 200, {"remote": True})
                return
            try:
                json_response(self, 200, {"path": choose_output_dir()})
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return
        if self.path == "/api/open-output-dir":
            client_ip = self.headers.get("X-Forwarded-For") or self.client_address[0]
            if client_ip not in ("127.0.0.1", "::1", "localhost"):
                json_response(self, 200, {"remote": True})
                return
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            try:
                json_response(self, 200, {"ok": True, "path": open_output_dir(get_field(form, "output_dir"))})
            except Exception as exc:
                json_response(self, 500, api_error("open_output_dir_failed", "打开输出目录失败", str(exc)))
            return
        if self.path == "/api/cleanup-cache":
            client_ip = self.headers.get("X-Forwarded-For") or self.client_address[0]
            if client_ip not in ("127.0.0.1", "::1", "localhost"):
                json_response(self, 200, {"remote": True})
                return
            try:
                json_response(self, 200, cleanup_cache())
            except Exception as exc:
                json_response(self, 500, api_error("cleanup_cache_failed", "清理缓存失败", str(exc)))
            return
        if self.path == "/api/workspace/snapshot":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            try:
                ws = _workspace_id(self)
                json_response(self, 200, preset_to_client(collect_workspace_snapshot_from_form(form, ws), ws))
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return
        if self.path == "/api/optimize-prompt":
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else "{}"
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                json_response(self, 400, {"ok": False, "error": "请求格式异常"})
                return
            json_response(self, 200, optimize_prompt(data.get("prompt", "")))
            return
        if self.path == "/api/preset":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            ws = _workspace_id(self)
            preset = collect_preset_from_form(form, ws)
            write_active_preset(preset, ws)
            archive_name = get_field(form, "archive_name")
            archive = None
            if archive_name.strip():
                archive = save_archive_file(archive_name, preset, ws).name
            response = preset_for_client(ws)
            response["archive"] = archive
            response["archives"] = list_archives(self)
            json_response(self, 200, response)
            return
        if self.path == "/api/media/upload":
            ws = _workspace_id(self)
            ctype = self.headers.get("Content-Type", "")
            if not ctype.startswith("multipart/form-data"):
                json_response(self, 400, {"error": "expected multipart/form-data"})
                return
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": ctype,
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
                keep_blank_values=True,
            )
            field_name = None
            file_item = None
            for key in form.keys():
                item = form[key]
                if isinstance(item, list):
                    item = item[0] if item else None
                if item is None:
                    continue
                fname = getattr(item, "filename", None)
                fobj = getattr(item, "file", None)
                if fname and fobj is not None:
                    field_name = key
                    file_item = item
                    break
            if not field_name or file_item is None:
                json_response(self, 400, {"error": "no file provided"})
                return
            filename = Path(file_item.filename).name
            data = file_item.file.read()
            if not data:
                json_response(self, 400, {"error": "empty file"})
                return
            # Enforce that the actual file bytes match the field's declared
            # media kind. Prevents evil.jpg-with-SVG-body upload → XSS on
            # any client that ever renders it as image.
            expected_kind = None
            if field_name.startswith("ref_image_"):
                expected_kind = "image"
            elif field_name.startswith("ref_video_"):
                expected_kind = "video"
            elif field_name.startswith("ref_audio_"):
                expected_kind = "audio"
            if expected_kind:
                actual = sniff_kind(data[:16])
                if actual != expected_kind:
                    json_response(self, 415, {
                        "ok": False,
                        "error": f"content does not match {expected_kind}: "
                                 f"detected {actual or 'unknown'}",
                    })
                    return
            suffix = Path(filename).suffix.lower()
            stored = f"{uuid.uuid4().hex}_{field_name}{suffix}"
            media_dir = _ws_media_dir(ws)
            media_dir.mkdir(parents=True, exist_ok=True)
            (media_dir / stored).write_bytes(data)
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            preset = read_preset(ws)
            media = preset.get("media") or {}
            old = media.get(field_name)
            if old and old.get("stored"):
                old_path = media_dir / old["stored"]
                try:
                    if old_path.exists() and old_path.name != stored:
                        old_path.unlink()
                except Exception:
                    pass
            media[field_name] = {"filename": filename, "mime": mime, "stored": stored}
            preset["media"] = media
            write_active_preset(preset, ws)
            url = f"/api/media/{urllib.parse.quote(stored)}?ws={urllib.parse.quote(ws)}&v={int(time.time())}"
            json_response(self, 200, {
                "ok": True,
                "field": field_name,
                "filename": filename,
                "mime": mime,
                "stored": stored,
                "url": url,
            })
            return
        if self.path == "/api/archive/load":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            try:
                data = load_archive_file(get_field(form, "archive_name"), self)
                data["archives"] = list_archives(self)
                # Merge archive data into current workspace media state
                ws = _workspace_id(self)
                media_dir = _ws_media_dir(ws)
                for item in (data.get("media") or {}).values():
                    stored = item.get("stored", "")
                    if stored and not (media_dir / stored).exists():
                        # Already extracted by load_archive_file
                        pass
                json_response(self, 200, data)
            except Exception as exc:
                json_response(self, 400, {"error": str(exc)})
            return
        if self.path == "/api/archive/delete":
            if not _is_admin(self):
                json_response(self, 403, {"ok": False, "error": "admin only"})
                return
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            path = archive_path(get_field(form, "archive_name"), _workspace_id(self))
            if path.exists():
                path.unlink()
            json_response(self, 200, {"archives": list_archives(self)})
            return
        if self.path == "/api/preset/clear":
            if not _is_admin(self):
                json_response(self, 403, {"ok": False, "error": "admin only"})
                return
            ws = _workspace_id(self)
            ws_dir = _ws_dir(ws)
            if ws_dir.exists():
                shutil.rmtree(ws_dir)
            json_response(self, 200, {"ok": True})
            return
        if self.path == "/api/jobs/json":
            try:
                payload = read_json_body(self)
                values, files = values_files_from_json(payload)
                # Provider is locked to volcengine — ignore any provider/api_key
                # the client passes and always use the company SECRETS key.
                values["provider"] = "volcengine"
                api_key = SECRETS["volcengine_api_key"]
                if not api_key and not payload.get("dry_run"):
                    json_response(self, 400, api_error("invalid_request", "API key is required"))
                    return
                if api_key:
                    values["api_key"] = api_key
                if payload.get("dry_run"):
                    response = {
                        "ok": True,
                        "dry_run": True,
                        "values": {k: ("***" if k == "api_key" else v) for k, v in values.items()},
                        "files": {k: {"filename": v[0], "bytes": len(v[1])} for k, v in files.items()},
                    }
                    record_activity({
                        "source": "api",
                        "request_kind": "json_dry_run",
                        "status": "succeeded",
                        "title": str(values.get("prompt") or "")[:80] or "Seedance dry run",
                        "request": summarize_payload(payload),
                        "response": response,
                        "username": _decode_username(self),
                    })
                    json_response(self, 200, response)
                    return
                request_data = {"raw": summarize_payload(payload), "parsed": summarize_values_files(values, files)}
                ws = _workspace_id(self)
                values["_request_host"] = _public_base_url(self) or ""
                job_id = create_job(values, files, "api", "json", request_data, ws, username=_decode_username(self))
                json_response(self, 200, job_id_response(job_id))
            except Exception as exc:
                json_response(self, 400, api_error("invalid_request", str(exc)))
            return
        if self.path != "/api/jobs":
            json_response(self, 404, {"error": "not found"})
            return
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
        # Provider is locked to volcengine — ignore any client-supplied api_key
        # / provider and always use the company SECRETS key.
        api_key = SECRETS["volcengine_api_key"]
        if not api_key:
            json_response(self, 400, api_error("invalid_request", "API key is required"))
            return

        form_values = {key: get_field(form, key) for key in form.keys() if not getattr(form[key], "filename", None)}
        form_values["api_key"] = api_key
        form_values["provider"] = "volcengine"
        form_files: dict[str, tuple[str, bytes]] = {}
        for key in form.keys():
            item = form[key]
            if getattr(item, "filename", None):
                blob = item.file.read()
                if blob:
                    form_files[key] = (Path(item.filename).name, blob)

        request_data = summarize_values_files(form_values, form_files)
        ws = _workspace_id(self)
        form_values["_request_host"] = _public_base_url(self) or ""
        job_id = create_job(form_values, form_files, "page", "multipart", request_data, ws, username=_decode_username(self))
        json_response(self, 200, job_id_response(job_id))


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REFMEDIA_DIR.mkdir(parents=True, exist_ok=True)
    # Wipe any leftover refmedia from a previous process — the token map is
    # in-memory only, so old files are unreachable. Then start the TTL sweeper.
    reset_refmedia_dir()
    threading.Thread(target=cleanup_refmedia_loop, daemon=True).start()
    # Restore persisted download token → file mappings (survives server restart)
    restored = load_files_map()
    if restored:
        FILES.update(restored)
        print(f"Restored {len(restored)} download file mapping(s)")
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Seedance GUI running at http://{host}:{port}")
    print("Press Ctrl+C to stop")

    def shutdown_handler(*args):
        print("\nShutting down...")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown_handler)
    elif hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
