#!/usr/bin/env python3
"""Volcengine Portrait — 真人人像 & 虚拟人像 独立子应用"""
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
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
_DATA_BASE = Path(os.environ.get("DATA_DIR", str(ROOT)))
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = _DATA_BASE / "outputs"
STATE_DIR = _DATA_BASE / "state"
LOG_DIR = _DATA_BASE / "logs"
UPLOAD_DIR = _DATA_BASE / "uploads"

for d in [OUTPUT_DIR, STATE_DIR, LOG_DIR, UPLOAD_DIR]:
    d.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8891"))
CORS = os.environ.get("CORS") == "1"

APP_NAME = "volcengine-portrait"
PORTAL_INTERNAL_TOKEN = os.environ.get("PORTAL_INTERNAL_TOKEN", "")
PORTAL_PORT_FOR_CALLBACK = int(os.environ.get("PORTAL_PORT", "9090"))
import ssl as _ssl
_PORTAL_SSL_CTX = _ssl.create_default_context()
_PORTAL_SSL_CTX.check_hostname = False
_PORTAL_SSL_CTX.verify_mode = _ssl.CERT_NONE


def _view_scope(handler) -> tuple[bool, str]:
    sees_all = handler.headers.get("X-Is-Admin", "") == "1"
    username = _decode_username(handler)
    return sees_all, username


def _decode_username(handler) -> str:
    """Portal injects X-Username via urllib.parse.quote()."""
    raw = (handler.headers.get("X-Username", "") or "").strip()
    if not raw:
        return ""
    try:
        return urllib.parse.unquote(raw)
    except Exception:
        return raw


def report_final_to_portal(job_id: str, status: str) -> None:
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

MAX_CONCURRENT = 2
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
API_KEY = ""
ACCESS_KEY = ""
SECRET_KEY = ""  # raw form (decoded from base64 if needed)

# ── TOS upload (for image/video/audio reference media in Ark tasks) ──────────
# Portal injects AK/SK via env from this app's config.json (same volcengine
# credentials power both Ark API calls and TOS uploads). tos_bucket / tos_region
# live in config.json alongside the other admin-managed fields. Both pieces
# must be present for reference media uploads to work — tos_upload raises a
# clear RuntimeError when anything is missing.
TOS_ACCESS_KEY = os.environ.get("TOS_ACCESS_KEY", "").strip()
TOS_SECRET_KEY = os.environ.get("TOS_SECRET_KEY", "").strip()
TOS_BUCKET = ""
TOS_REGION = ""
TOS_DEFAULT_REGION = "cn-beijing"


def _tos_sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tos_hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _tos_sign_put(bucket: str, region: str, object_key: str, mime: str, body: bytes) -> dict[str, str]:
    """TOS PutObject SigV4-style signing. Algorithm string is `TOS4-HMAC-SHA256`
    (NOT the AWS variant) and headers use the `x-tos-*` namespace."""
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
    """Query-string-signed GET URL for a private TOS object. Default 12h TTL."""
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
    """Upload to the configured TOS bucket, return public https URL.
    Raises RuntimeError on any precondition or transport failure."""
    if not (TOS_ACCESS_KEY and TOS_SECRET_KEY):
        raise RuntimeError(
            "TOS 凭证未配置：请在 Portal 管理员菜单 →「火山方舟人像 Key」处配置 AK/SK，"
            "重启 Portal 后子应用会自动继承"
        )
    bucket = (TOS_BUCKET or "").strip()
    if not bucket:
        raise RuntimeError(
            "volcengine-portrait/config.json 缺 'tos_bucket'：请在 config.json 里填入 bucket 名后重启"
        )
    region = (TOS_REGION or TOS_DEFAULT_REGION).strip()
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
            raise RuntimeError(f"TOS 上传连接失败: {type(exc).__name__}: {exc}") from exc
        if resp.status not in (200, 201):
            body_err = resp.read()[:500].decode("utf-8", errors="replace")
            raise RuntimeError(f"TOS upload HTTP {resp.status}: {body_err}")
        resp.read()
    finally:
        conn.close()
    # Bucket is private; return a 12-hour presigned GET URL so Ark can fetch it.
    return _tos_presigned_get_url(bucket, region, object_key, expires=43200)


def load_config():
    global MAX_CONCURRENT, ARK_BASE_URL, API_KEY, ACCESS_KEY, SECRET_KEY, OUTPUT_DIR, TOS_BUCKET, TOS_REGION
    cfg_path = ROOT / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text("utf-8"))
            MAX_CONCURRENT = cfg.get("max_concurrent", 2)
            ARK_BASE_URL = cfg.get("base_url", ARK_BASE_URL)
            API_KEY = cfg.get("api_key", "")
            ACCESS_KEY = cfg.get("access_key", "")
            raw_sk = cfg.get("secret_key", "")
            if raw_sk:
                SECRET_KEY = raw_sk
            TOS_BUCKET = (cfg.get("tos_bucket") or "").strip()
            TOS_REGION = (cfg.get("tos_region") or "").strip()
            if cfg.get("output_dir"):
                p = Path(cfg["output_dir"])
                p.mkdir(parents=True, exist_ok=True)
                OUTPUT_DIR = p
        except Exception:
            pass


def save_config(updates: dict):
    """Save partial config updates to config.json, reload affected globals."""
    global OUTPUT_DIR
    cfg_path = ROOT / "config.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text("utf-8"))
        except Exception:
            pass
    cfg.update(updates)
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    if "output_dir" in updates:
        p = Path(updates["output_dir"])
        p.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR = p


def choose_output_dir() -> str:
    """Open a native OS directory picker, return selected path."""
    prompt = "选择人像生成输出目录"
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
    # Linux / other: tkinter fallback
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


# === Data models (in-memory) ===
GROUPS: dict[str, dict] = {}
ASSETS: dict[str, dict] = {}
JOBS: dict[str, dict] = {}
FILES: dict[str, Path] = {}
FILES_MAP_PATH = STATE_DIR / "download_files.json"
ACTIVITY_PATH = STATE_DIR / "activity_log.json"
ACTIVITY_LIMIT = 500

JOBS_LOCK = threading.Lock()
GROUP_LOCK = threading.Lock()
ASSET_LOCK = threading.Lock()
FILES_LOCK = threading.Lock()
ACTIVITY_LOCK = threading.Lock()


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def read_activity_log() -> list[dict]:
    if not ACTIVITY_PATH.exists():
        return []
    try:
        data = json.loads(ACTIVITY_PATH.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_activity_log(items: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    content = json.dumps(items[-ACTIVITY_LIMIT:], ensure_ascii=False, indent=2)
    with ACTIVITY_LOCK:
        _atomic_write(ACTIVITY_PATH, content)


def record_activity(record: dict, ws_id: str = "localhost") -> None:
    with ACTIVITY_LOCK:
        items = read_activity_log()
        record.setdefault("id", uuid.uuid4().hex)
        record.setdefault("created_at", _now_text())
        record.setdefault("updated_at", record["created_at"])
        record["workspace_id"] = ws_id
        items.append(record)
        content = json.dumps(items[-ACTIVITY_LIMIT:], ensure_ascii=False, indent=2)
        _atomic_write(ACTIVITY_PATH, content)


def update_activity(activity_id: str | None, **updates) -> None:
    if not activity_id:
        return
    with ACTIVITY_LOCK:
        items = read_activity_log()
        for item in items:
            if item.get("id") == activity_id:
                item.update(updates)
                item["updated_at"] = _now_text()
                content = json.dumps(items[-ACTIVITY_LIMIT:], ensure_ascii=False, indent=2)
                _atomic_write(ACTIVITY_PATH, content)
                return


def activity_list(sees_all: bool = True, username: str = "") -> dict:
    items = read_activity_log()
    if not sees_all and username:
        items = [it for it in items if it.get("username", "") == username]
    counts = {"total": len(items), "page": 0, "api": 0,
              "succeeded": 0, "failed": 0, "running": 0, "queued": 0}
    summary = []
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
        })
    summary.reverse()
    return {"counts": counts, "records": summary}


def activity_record_for_client(record: dict | None) -> dict | None:
    if not record:
        return None
    return json.loads(json.dumps(record))


def load_files_map() -> dict[str, Path]:
    """Load persisted download-token → file-path mapping from disk."""
    try:
        if FILES_MAP_PATH.exists():
            data = json.loads(FILES_MAP_PATH.read_text("utf-8"))
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
        with FILES_LOCK:
            data = {token: str(p) for token, p in FILES.items()}
        tmp = FILES_MAP_PATH.with_suffix(FILES_MAP_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(FILES_MAP_PATH)
    except Exception:
        pass


load_config()
FILES.update(load_files_map())

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT)


def handle_config_post(handler):
    """Update runtime config (output_dir, and admin-only api_key/access_key/secret_key)."""
    data = read_json_body(handler)
    updates = {}
    is_admin = handler.headers.get("X-Is-Admin") == "1"
    if "output_dir" in data:
        p = (data["output_dir"] or "").strip()
        if not p:
            json_response(handler, 400, {"ok": False, "error": "output_dir cannot be empty"})
            return
        updates["output_dir"] = p
    # Admin-only: write the company-wide key/AK/SK.
    # Empty strings are silently ignored (interpreted as "do not modify");
    # to clear a key, edit config.json directly. This avoids accidental wipe.
    key_fields_attempted = any(k in data for k in ("api_key", "access_key", "secret_key"))
    if key_fields_attempted:
        if not is_admin:
            json_response(handler, 403, {"ok": False, "error": "admin only"})
            return
        for field in ("api_key", "access_key", "secret_key"):
            if field in data:
                val = (data[field] or "").strip()
                if val:
                    updates[field] = val
    if not updates:
        json_response(handler, 400, {"ok": False, "error": "no valid config fields"})
        return
    save_config(updates)
    # Reload globals so fallback in ark_v3_call / openapi_call uses the new key
    # immediately, without restarting the subapp.
    load_config()
    json_response(handler, 200, {
        "ok": True,
        "output_dir": str(OUTPUT_DIR),
        "has_api_key": bool(API_KEY),
        "has_access_key": bool(ACCESS_KEY),
        "has_secret_key": bool(SECRET_KEY),
    })


def _public(d):
    """Return a copy of dict without internal fields."""
    return {k: v for k, v in d.items() if k not in ("api_key", "access_key", "secret_key")}


def json_response(handler, status, data):
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
    if CORS:
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Expose-Headers", "X-Job-Id")
    handler.end_headers()
    try:
        handler.wfile.write(raw)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length") or "0")
    if length == 0:
        return {}
    try:
        return json.loads(handler.rfile.read(length))
    except Exception:
        return {}


# === Volcengine SigV4 signing for OpenAPI (Asset API) ===

def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(s):
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.sha256(s).hexdigest()


def _openapi_v4_sign(ak, sk, method, host, uri, query, headers, payload):
    """Return (Authorization header value, X-Date value)."""
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    region = "cn-beijing"
    service = "ark"

    payload_hash = _sha256_hex(payload or "")

    headers["Host"] = host
    headers["X-Date"] = amz_date
    headers["X-Content-Sha256"] = payload_hash
    if payload:
        headers["Content-Type"] = "application/json"

    # Canonical headers (sorted by header name, case-insensitive)
    canonical_headers = ""
    signed_headers_list = []
    for k in sorted(headers.keys(), key=str.lower):
        kl = k.lower()
        canonical_headers += f"{kl}:{headers[k].strip()}\n"
        signed_headers_list.append(kl)
    signed_headers = ";".join(signed_headers_list)

    canonical_request = (
        f"{method}\n{uri}\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    credential_scope = f"{date_stamp}/{region}/{service}/request"
    string_to_sign = (
        f"HMAC-SHA256\n{amz_date}\n{credential_scope}\n{_sha256_hex(canonical_request)}"
    )

    k_date = _sign(sk.encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return authorization, amz_date


PROJECT_NAME = "Seedance2.0"


def openapi_call(action, body, ak=None, sk=None, timeout=120):
    """Call Volcengine OpenAPI (Asset API) with AK/SK SigV4 signing."""
    ak = ak or ACCESS_KEY
    sk = sk or SECRET_KEY
    if not ak or not sk:
        return {"error": "Missing AK/SK"}

    method = "POST"
    host = "ark.cn-beijing.volcengineapi.com"
    uri = "/"
    query = f"Action={action}&Version=2024-01-01"

    payload_str = json.dumps(body) if body else ""
    headers = {}
    authorization, amz_date = _openapi_v4_sign(ak, sk, method, host, uri, query, headers, payload_str)
    headers["Authorization"] = authorization

    url = f"https://{host}/?{query}"
    data = payload_str.encode("utf-8") if payload_str else None
    # Pass headers via constructor so urllib doesn't auto-inject a conflicting Content-Type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    # Debug: log the signing details
    print(f"[openapi_call] Action={action} AK={ak[:8]}... SK[0:4]={sk[:4]}... SK len={len(sk)}", flush=True)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            print(f"[openapi_call] SUCCESS Action={action}", flush=True)
            return result
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode(errors="replace")[:800]
        except Exception:
            pass
        print(f"[openapi_call] FAIL Action={action} HTTP={e.code} detail={err_body}", flush=True)
        return {"error": f"HTTP {e.code}", "detail": err_body}
    except Exception as e:
        print(f"[openapi_call] EXCEPTION Action={action}: {e}", flush=True)
        return {"error": str(e)}


def openapi_result(response):
    """Return the business Result object from a Volcengine OpenAPI response."""
    if isinstance(response, dict) and isinstance(response.get("Result"), dict):
        return response["Result"]
    return response if isinstance(response, dict) else {}


# === Ark API v3 calls (Bearer token) for video generation ===

def ark_v3_call(method, path, body=None, timeout=120, api_key=None):
    """Call Ark API v3 (video generation, files) with Bearer token."""
    url = f"{ARK_BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {"Authorization": f"Bearer {api_key or API_KEY}"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode(errors="replace")[:500]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}", "detail": err_body}
    except Exception as e:
        return {"error": str(e)}


def upload_file_to_ark(file_data, filename, mime_type, api_key=None):
    """Upload a file to Ark Files API, return (file_id, file_url) or (None, None)."""
    boundary = uuid.uuid4().hex
    body = b""
    # purpose field
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="purpose"\r\n\r\n'
    body += b"user_data\r\n"
    # file field
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += f"Content-Type: {mime_type}\r\n\r\n".encode()
    body += file_data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    url = f"{ARK_BASE_URL}/files"
    headers = {
        "Authorization": f"Bearer {api_key or API_KEY}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            fid = result.get("id") or result.get("file_id", "")
            fname = result.get("filename", filename)
            return fid, fname
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode(errors="replace")[:500]
        except Exception:
            pass
        print(f"  [ERROR] upload_file_to_ark: HTTP {e.code}: {err_body}")
        return None, None
    except Exception as e:
        print(f"  [ERROR] upload_file_to_ark: {e}")
        return None, None


# === Background: Asset Status Polling ===

def poll_asset_status(asset_id, ak=None, sk=None):
    """Poll asset status via GetAsset action until Active/Failed."""

    for _ in range(120):
        time.sleep(5)
        result = openapi_call("GetAsset", {"Id": asset_id, "ProjectName": PROJECT_NAME}, ak=ak, sk=sk)
        if "error" in result:
            with ASSET_LOCK:
                if asset_id in ASSETS:
                    ASSETS[asset_id]["status"] = "error"
                    ASSETS[asset_id]["error"] = result["error"]
            return
        item = openapi_result(result)
        status = (item.get("Status") or "").lower()
        with ASSET_LOCK:
            if asset_id in ASSETS:
                ASSETS[asset_id]["status"] = status
                ASSETS[asset_id]["raw_latest"] = result
        if status == "active":
            return
        if status in ("failed", "error"):
            return


# === Download helper ===

def download_video(video_url, job_id, idx):
    try:
        req = urllib.request.Request(video_url)
        with urllib.request.urlopen(req, timeout=300) as resp:
            ext = mimetypes.guess_extension(resp.headers.get("Content-Type", "video/mp4")) or ".mp4"
            fname = f"{job_id}_{idx}{ext}"
            fpath = OUTPUT_DIR / fname
            fpath.write_bytes(resp.read())
            return fpath
    except Exception as e:
        print(f"  [ERROR] download_video: {e}")
        return None


def extract_video_url(data: dict[str, Any]) -> str | None:
    """Extract video URL from Ark API task result (handles multiple response shapes)."""
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
    for key in ("video_url", "videoUrl"):
        val = data.get(key)
        if isinstance(val, str):
            return val
    output = data.get("output")
    if isinstance(output, dict):
        url = output.get("video_url") or output.get("videoUrl")
        if url:
            return str(url)
    results = data.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
    return None


# === Virtual Portrait Handlers ===

def handle_virtual_groups_post(handler):
    data = read_json_body(handler)
    name = (data.get("name") or "").strip() or f"group-{time.strftime('%Y%m%d-%H%M%S')}"
    description = (data.get("description") or "").strip()
    ak = None  # company-wide; admin-managed via /api/config (X-Is-Admin)
    sk = None

    body = {"Name": name, "ProjectName": PROJECT_NAME, "GroupType": "AIGC"}
    if description:
        body["Description"] = description
    result = openapi_call("CreateAssetGroup", body, ak=ak, sk=sk)
    if "error" in result:
        code = 401 if "Missing AK/SK" in result.get("error", "") else 502
        json_response(handler, code, {"ok": False, "error": result["error"], "detail": result.get("detail")})
        return

    item = openapi_result(result)
    gid = item.get("Id") or item.get("GroupId", "")
    if not gid:
        json_response(handler, 502, {"ok": False, "error": "no Id in response", "detail": str(result)[:200]})
        return
    with GROUP_LOCK:
        GROUPS[gid] = {
            "group_id": gid,
            "name": name,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "raw": result,
        }
    json_response(handler, 200, {"ok": True, "group_id": gid})


def handle_virtual_groups_get(handler):
    """List asset groups via ListAssetGroups."""
    ak = None  # company-wide; admin-managed via /api/config (X-Is-Admin)
    sk = None

    parsed_url = urllib.parse.urlparse(handler.path)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    filter_body = {"GroupType": "AIGC"}
    if "name" in query_params and query_params["name"][0].strip():
        filter_body["Name"] = query_params["name"][0].strip()
    if "group_ids" in query_params and query_params["group_ids"][0].strip():
        filter_body["GroupIds"] = [g.strip() for g in query_params["group_ids"][0].split(",") if g.strip()]

    result = openapi_call("ListAssetGroups", {
        "Filter": filter_body,
        "PageNumber": int(query_params.get("page", ["1"])[0]),
        "PageSize": int(query_params.get("page_size", ["50"])[0]),
        "ProjectName": PROJECT_NAME,
    }, ak=ak, sk=sk)
    if "error" in result:
        code = 401 if "Missing AK/SK" in result.get("error", "") else 502
        json_response(handler, code, {"ok": False, "error": result["error"], "detail": result.get("detail")})
        return

    items = openapi_result(result).get("Items") or []
    groups = []
    for item in items:
        groups.append({
            "group_id": item.get("Id", ""),
            "name": item.get("Name", ""),
            "description": item.get("Description", ""),
            "project_name": item.get("ProjectName", ""),
            "created_at": item.get("CreateTime", ""),
        })
    # Also merge with local cache
    with GROUP_LOCK:
        for gid, g in GROUPS.items():
            if not any(x["group_id"] == gid for x in groups):
                groups.append(g)
    json_response(handler, 200, {"ok": True, "groups": groups})


def _upload_to_public_host(file_data, filename, mime_type):
    """Upload a file to a public host to get an HTTP URL accessible by CreateAsset.
    Tries multiple free hosts, returns the public URL or None."""
    boundary = uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="files[]"; filename="{filename}"\r\n'.encode()
    body += f"Content-Type: {mime_type}\r\n\r\n".encode()
    body += file_data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    # Try uguu.se first (returns direct URL)
    try:
        req = urllib.request.Request(
            "https://uguu.se/upload.php",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        files = result.get("files") or []
        if files and files[0].get("url"):
            url = files[0]["url"]
            print(f"  [public_upload] uguu.se OK → {url}", flush=True)
            return url
    except Exception as e:
        print(f"  [public_upload] uguu.se FAIL: {e}", flush=True)

    return None


def handle_virtual_assets_post(handler):
    content_type = handler.headers.get("Content-Type", "")
    if "multipart" not in content_type:
        json_response(handler, 400, {"ok": False, "error": "multipart required"})
        return
    cl = handler.headers.get("Content-Length", "0")
    form = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers,
                            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type, "CONTENT_LENGTH": cl})
    group_id = form.getfirst("group_id", "")
    if not group_id:
        json_response(handler, 400, {"ok": False, "error": "group_id required"})
        return

    files = []
    for key in form.keys():
        item = form[key]
        if item.filename:
            files.append((item.filename, item.file.read(), item.type or "application/octet-stream"))

    if not files:
        json_response(handler, 400, {"ok": False, "error": "no files uploaded"})
        return

    ak = None  # company-wide; admin-managed via /api/config (X-Is-Admin)
    sk = None
    api_key = None

    fname, fdata, fmime = files[0]

    # Determine asset type
    asset_type = "Image"
    if fmime.startswith("video/"):
        asset_type = "Video"
    elif fmime.startswith("audio/"):
        asset_type = "Audio"

    # Upload to a public host to get an accessible URL for CreateAsset
    source_url = _upload_to_public_host(fdata, fname, fmime)
    if not source_url:
        json_response(handler, 502, {"ok": False, "error": "failed to get public URL for file"})
        return

    # Call CreateAsset via OpenAPI
    create_body = {
        "GroupId": group_id,
        "URL": source_url,
        "AssetType": asset_type,
        "ProjectName": PROJECT_NAME,
    }
    if data_name := (form.getfirst("name") or "").strip():
        create_body["Name"] = data_name

    result = openapi_call("CreateAsset", create_body, ak=ak, sk=sk)
    if "error" in result:
        code = 401 if "Missing AK/SK" in result.get("error", "") else 502
        json_response(handler, code, {"ok": False, "error": result["error"], "detail": result.get("detail")})
        return

    item = openapi_result(result)
    asset_id = item.get("Id") or item.get("AssetId", "")
    if not asset_id:
        json_response(handler, 502, {"ok": False, "error": "no Id in CreateAsset response", "detail": str(result)[:200]})
        return

    with ASSET_LOCK:
        ASSETS[asset_id] = {
            "asset_id": asset_id,
            "group_id": group_id,
            "status": "processing",
            "file_name": fname,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "raw": result,
        }
    threading.Thread(target=poll_asset_status, args=(asset_id, ak, sk), daemon=True).start()
    json_response(handler, 200, {"ok": True, "asset_id": asset_id})


def handle_virtual_assets_get(handler, asset_id=None):
    ak = None  # company-wide; admin-managed via /api/config (X-Is-Admin)
    sk = None

    if asset_id:
        with ASSET_LOCK:
            local = ASSETS.get(asset_id)
        # Fetch latest from API
        result = openapi_call("GetAsset", {"Id": asset_id, "ProjectName": PROJECT_NAME}, ak=ak, sk=sk)
        if "error" not in result:
            item = openapi_result(result)
            with ASSET_LOCK:
                if asset_id in ASSETS:
                    ASSETS[asset_id]["status"] = (item.get("Status") or "").lower()
                    ASSETS[asset_id]["url"] = item.get("URL", "")
                    ASSETS[asset_id]["raw_latest"] = result
        if local:
            json_response(handler, 200, {"ok": True, **_public(local)})
        else:
            json_response(handler, 404, {"ok": False, "error": "asset not found"})
    else:
        # Fetch assets from Volcengine ListAssets API
        api_assets = []
        parsed_url = urllib.parse.urlparse(handler.path)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        filter_body = {"GroupType": "AIGC", "Statuses": ["Active", "Processing", "Failed"]}
        if "group_ids" in query_params and query_params["group_ids"][0].strip():
            filter_body["GroupIds"] = [g.strip() for g in query_params["group_ids"][0].split(",") if g.strip()]
        if "name" in query_params and query_params["name"][0].strip():
            filter_body["Name"] = query_params["name"][0].strip()
        list_assets_body = {
            "Filter": filter_body,
            "PageNumber": int(query_params.get("page", ["1"])[0]),
            "PageSize": int(query_params.get("page_size", ["50"])[0]),
            "ProjectName": PROJECT_NAME,
        }
        if "sort_by" in query_params and query_params["sort_by"][0].strip():
            list_assets_body["SortBy"] = query_params["sort_by"][0].strip()
        if "sort_order" in query_params and query_params["sort_order"][0].strip():
            list_assets_body["SortOrder"] = query_params["sort_order"][0].strip()
        result = openapi_call("ListAssets", list_assets_body, ak=ak, sk=sk)
        if "error" not in result:
            for item in openapi_result(result).get("Items") or []:
                aid = item.get("Id") or item.get("AssetId", "")
                api_assets.append({
                    "asset_id": aid,
                    "group_id": item.get("GroupId", ""),
                    "file_name": item.get("Name") or item.get("FileName", ""),
                    "status": (item.get("Status") or "unknown").lower(),
                    "created_at": item.get("CreateTime", ""),
                    "asset_type": item.get("AssetType", "Image"),
                    "url": item.get("URL", ""),
                })
                # Update in-memory cache
                with ASSET_LOCK:
                    if aid and aid not in ASSETS:
                        ASSETS[aid] = api_assets[-1]
        # Merge with local cache
        with ASSET_LOCK:
            local = [_public(a) for a in ASSETS.values()]
        # Merge: API results first, then local items not in API results
        api_ids = {a["asset_id"] for a in api_assets}
        merged = api_assets.copy()
        for a in local:
            if a.get("asset_id") not in api_ids:
                merged.append(a)
        merged.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        total = openapi_result(result).get("TotalCount", len(merged))
        json_response(handler, 200, {"ok": True, "assets": merged, "total_count": total})


def handle_virtual_assets_delete(handler, asset_id):
    ak = None  # company-wide; admin-managed via /api/config (X-Is-Admin)
    sk = None

    with ASSET_LOCK:
        asset = ASSETS.pop(asset_id, None)
    if not ACCESS_KEY or not SECRET_KEY:
        json_response(handler, 401, {"ok": False, "error": "服务端未配置 AK/SK,请联系管理员在 portal 统计页配置"})
        return
    result = openapi_call("DeleteAsset", {"Id": asset_id, "ProjectName": PROJECT_NAME}, ak=ak, sk=sk)
    if "error" in result:
        code = 401 if "Missing AK/SK" in result.get("error", "") else 502
        json_response(handler, code, {"ok": False, "error": result["error"], "detail": result.get("detail")})
        return
    json_response(handler, 200, {"ok": True})


def handle_virtual_group_get(handler, group_id):
    """Get a single asset group via GetAssetGroup."""
    ak = None  # company-wide; admin-managed via /api/config (X-Is-Admin)
    sk = None

    result = openapi_call("GetAssetGroup", {"Id": group_id, "ProjectName": PROJECT_NAME}, ak=ak, sk=sk)
    if "error" in result:
        code = 401 if "Missing AK/SK" in result.get("error", "") else 502
        json_response(handler, code, {"ok": False, "error": result["error"], "detail": result.get("detail")})
        return

    item = openapi_result(result)
    group = {
        "group_id": item.get("Id", ""),
        "name": item.get("Name", ""),
        "description": item.get("Description", ""),
        "project_name": item.get("ProjectName", ""),
        "group_type": item.get("GroupType", ""),
        "created_at": item.get("CreateTime", ""),
        "updated_at": item.get("UpdateTime", ""),
    }
    json_response(handler, 200, {"ok": True, "group": group})


def handle_virtual_group_update(handler, group_id):
    """Update an asset group via UpdateAssetGroup."""
    data = read_json_body(handler)
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    ak = None  # company-wide; admin-managed via /api/config (X-Is-Admin)
    sk = None

    if not name:
        json_response(handler, 400, {"ok": False, "error": "name is required"})
        return

    body = {"Id": group_id, "Name": name, "ProjectName": PROJECT_NAME}
    if description:
        body["Description"] = description
    result = openapi_call("UpdateAssetGroup", body, ak=ak, sk=sk)
    if "error" in result:
        code = 401 if "Missing AK/SK" in result.get("error", "") else 502
        json_response(handler, code, {"ok": False, "error": result["error"], "detail": result.get("detail")})
        return

    with GROUP_LOCK:
        if group_id in GROUPS:
            GROUPS[group_id]["name"] = name
            if description:
                GROUPS[group_id]["description"] = description
    json_response(handler, 200, {"ok": True, "group_id": group_id})


def handle_virtual_asset_update(handler, asset_id):
    """Update an asset name via UpdateAsset."""
    data = read_json_body(handler)
    name = (data.get("name") or "").strip()
    ak = None  # company-wide; admin-managed via /api/config (X-Is-Admin)
    sk = None

    if not name:
        json_response(handler, 400, {"ok": False, "error": "name is required"})
        return

    result = openapi_call("UpdateAsset", {"Id": asset_id, "Name": name, "ProjectName": PROJECT_NAME}, ak=ak, sk=sk)
    if "error" in result:
        code = 401 if "Missing AK/SK" in result.get("error", "") else 502
        json_response(handler, code, {"ok": False, "error": result["error"], "detail": result.get("detail")})
        return

    with ASSET_LOCK:
        if asset_id in ASSETS:
            ASSETS[asset_id]["file_name"] = name
    json_response(handler, 200, {"ok": True, "asset_id": asset_id})


def handle_virtual_group_delete(handler, group_id):
    """Delete an asset group via DeleteAssetGroup."""
    ak = None  # company-wide; admin-managed via /api/config (X-Is-Admin)
    sk = None

    result = openapi_call("DeleteAssetGroup", {"Id": group_id, "ProjectName": PROJECT_NAME}, ak=ak, sk=sk)
    if "error" in result:
        code = 401 if "Missing AK/SK" in result.get("error", "") else 502
        json_response(handler, code, {"ok": False, "error": result["error"], "detail": result.get("detail")})
        return

    with GROUP_LOCK:
        GROUPS.pop(group_id, None)
    json_response(handler, 200, {"ok": True})


def handle_virtual_jobs_post(handler, task_type: str = "virtual"):
    content_type = handler.headers.get("Content-Type", "")

    if "multipart" in content_type:
        # Multipart mode: form fields + optional extra image files
        cl = handler.headers.get("Content-Length", "0")
        form = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers,
                                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type, "CONTENT_LENGTH": cl})
        asset_id = form.getfirst("asset_id", "")
        extra_asset_ids_raw = form.getfirst("extra_asset_ids", "") or "[]"
        try:
            extra_asset_ids = json.loads(extra_asset_ids_raw)
            if not isinstance(extra_asset_ids, list):
                extra_asset_ids = []
        except (ValueError, TypeError):
            extra_asset_ids = []
        prompt = form.getfirst("prompt", "")
        model = form.getfirst("model", "doubao-seedance-2-0-260128")
        duration = int(form.getfirst("duration", "12"))
        resolution = form.getfirst("resolution", "720p")
        ratio = form.getfirst("ratio", "16:9")
        repeat_count = int(form.getfirst("repeat_count", "1"))

        extra_files = []
        for key in form.keys():
            item = form[key]
            # cgi.FieldStorage returns a list when the same field name has
            # multiple values (e.g. <input multiple>). Single uploads come
            # back as a single FieldStorage with .filename.
            items = item if isinstance(item, list) else [item]
            for sub in items:
                if getattr(sub, "filename", None):
                    extra_files.append({
                        "filename": sub.filename,
                        "data": sub.file.read(),
                        "mime_type": sub.type or "application/octet-stream",
                    })
    else:
        # JSON mode (backward compatible)
        data = read_json_body(handler)
        asset_id = data.get("asset_id", "")
        extra_asset_ids = data.get("extra_asset_ids", [])
        if not isinstance(extra_asset_ids, list):
            extra_asset_ids = []
        prompt = data.get("prompt", "")
        model = data.get("model", "doubao-seedance-2-0-260128")
        duration = int(data.get("duration", 12))
        resolution = data.get("resolution", "720p")
        ratio = data.get("ratio", "16:9")
        repeat_count = int(data.get("repeat_count", 1))
        extra_files = []

    if not asset_id or not prompt:
        json_response(handler, 400, {"ok": False, "error": "asset_id and prompt required"})
        return

    api_key = None

    # Local "图2 上传本地图" extras: PUT each blob to the company TOS bucket and
    # pass the public https URL to Ark. (Asset library uploads still go through
    # the CreateAsset flow — they're separate routes.)
    extra_image_urls = []
    if extra_files:
        for ef in extra_files:
            try:
                public_url = tos_upload(ef["data"], ef["mime_type"], ef["filename"])
            except RuntimeError as exc:
                json_response(handler, 502, {"ok": False, "error": str(exc)})
                return
            extra_image_urls.append({
                "url": public_url,
                "filename": ef["filename"],
                "mime_type": ef["mime_type"],
            })

    job_id = uuid.uuid4().hex[:12]
    activity_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "activity_id": activity_id,
            "task_type": task_type,
            "asset_id": asset_id,
            "extra_asset_ids": extra_asset_ids,
            "prompt": prompt,
            "model": model,
            "duration": duration,
            "resolution": resolution,
            "ratio": ratio,
            "status": "queued",
            "total": repeat_count,
            "done": 0,
            "results": [],
            "errors": [],
            "extra_image_urls": extra_image_urls,
            "events": [{"time": time.strftime("%H:%M:%S"), "message": "任务已创建"}],
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "submitted_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "username": _decode_username(handler),
            "api_key": api_key,
        }
    title = (prompt or "").strip()[:80] or f"{task_type} task"
    record_activity({
        "id": activity_id,
        "job_id": job_id,
        "source": "page",
        "request_kind": task_type,
        "status": "running",
        "title": title,
        "username": _decode_username(handler),
        "request": {
            "task_type": task_type,
            "asset_id": asset_id,
            "extra_asset_ids": extra_asset_ids,
            "prompt": prompt,
            "model": model,
            "duration": duration,
            "resolution": resolution,
            "ratio": ratio,
            "repeat_count": repeat_count,
            "extra_image_count": len(extra_image_urls),
        },
        "response": {"job_id": job_id},
    })
    _executor.submit(run_virtual_job, job_id)
    json_response(handler, 201, {"ok": True, "job_id": job_id})


def handle_virtual_jobs_get(handler, job_id=None):
    if job_id:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            data = _public(json.loads(json.dumps(job))) if job else None
        json_response(handler, 200 if data else 404,
                      data or {"ok": False, "error": "job not found"})
    else:
        sees_all, username = _view_scope(handler)
        with JOBS_LOCK:
            jobs = [_public(j) for j in JOBS.values()]
        if not sees_all:
            jobs = [j for j in jobs if j.get("username", "") == username]
        jobs.sort(key=lambda j: (j.get("submitted_at") or 0), reverse=True)
        json_response(handler, 200, {"ok": True, "jobs": jobs[:50]})


# === Video generation job runner (Ark v3 API) ===

def run_virtual_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return
    try:
        _run_virtual_job_impl(job_id, job)
    except Exception as exc:
        with JOBS_LOCK:
            job["status"] = "failed"
            job["finished_at"] = time.time()
            job.setdefault("errors", []).append(f"fatal: {exc}")
            job.setdefault("events", []).append({"time": time.strftime("%H:%M:%S"), "message": f"任务异常: {exc}"})
        try:
            update_activity(job.get("activity_id"), status="failed", error=str(exc), result={
                "status": "failed",
                "done": job.get("done", 0),
                "total": job.get("total", 0),
                "results": list(job.get("results", [])),
                "errors": list(job.get("errors", [])),
            })
        except Exception:
            pass
        report_final_to_portal(job_id, "failed")
        return
    # _run_virtual_job_impl set the final job["status"] (succeeded or failed).
    with JOBS_LOCK:
        final_status = job.get("status", "")
    report_final_to_portal(job_id, final_status)


def _run_virtual_job_impl(job_id, job):
    api_key = job.get("api_key")
    asset_id = job.get("asset_id", "")
    asset_id_2 = job.get("asset_id_2", "")
    prompt = job.get("prompt", "")
    model = job.get("model", "doubao-seedance-2-0-260128")
    duration = int(job.get("duration", 12))
    resolution = job.get("resolution", "720p")
    ratio = job.get("ratio", "16:9")
    repeat_count = int(job.get("total", 1))
    extra_image_urls = job.get("extra_image_urls", [])

    with JOBS_LOCK:
        job["status"] = "running"
        job["started_at"] = time.time()
        job["events"].append({"time": time.strftime("%H:%M:%S"), "message": "开始提交生成任务..."})

    for idx in range(repeat_count):
        # Build content array: text prompt + reference images
        images = []
        # 图1: asset_id (required)
        images.append({"type": "image_url", "image_url": {"url": f"asset://{asset_id}"}, "role": "reference_image"})

        # 图2+: asset_id_2 takes priority, then extra uploaded images
        if asset_id_2:
            images.append({"type": "image_url", "image_url": {"url": f"asset://{asset_id_2}"}, "role": "reference_image"})
        elif extra_image_urls:
            for eiu in extra_image_urls:
                mt = (eiu.get("mime_type") or "image/png").lower()
                if mt.startswith("video/"):
                    images.append({"type": "video_url", "video_url": {"url": eiu["url"]}, "role": "reference_video"})
                elif mt.startswith("audio/"):
                    images.append({"type": "audio_url", "audio_url": {"url": eiu["url"]}, "role": "reference_audio"})
                else:
                    images.append({"type": "image_url", "image_url": {"url": eiu["url"]}, "role": "reference_image"})

        body = {
            "model": model,
            "content": [{"type": "text", "text": prompt}] + images,
            "duration": duration,
            "resolution": resolution,
            "ratio": ratio,
        }
        result = ark_v3_call("POST", "/contents/generations/tasks", body, timeout=120, api_key=api_key)
        task_id = result.get("id") or result.get("task_id", "")
        if "error" in result:
            with JOBS_LOCK:
                job["errors"].append(f"Run {idx}: {result['error']}")
                job["done"] += 1
                job["events"].append({"time": time.strftime("%H:%M:%S"), "message": f"Run {idx} 提交失败: {result['error']}"})
            continue

        with JOBS_LOCK:
            job["events"].append({"time": time.strftime("%H:%M:%S"), "message": f"Run {idx} 已提交 task={task_id}"})

        for _ in range(240):
            time.sleep(5)
            task_result = ark_v3_call("GET", f"/contents/generations/tasks/{task_id}", api_key=api_key)
            t_status = (task_result.get("status") or "").lower()
            if t_status in ("completed", "succeeded"):
                video_url = extract_video_url(task_result) or ""
                if video_url:
                    local_path = download_video(video_url, job_id, idx)
                    file_token = uuid.uuid4().hex
                    if local_path:
                        with FILES_LOCK:
                            FILES[file_token] = local_path
                        save_files_map()
                    with JOBS_LOCK:
                        job["results"].append({
                            "index": idx,
                            "task_id": task_id,
                            "filename": local_path.name if local_path else f"output_{idx}.mp4",
                            "download_url": f"/api/download/{file_token}" if local_path else video_url,
                            "status": "succeeded",
                        })
                        job["done"] += 1
                        job["events"].append({"time": time.strftime("%H:%M:%S"), "message": f"Run {idx} 完成"})
                else:
                    with JOBS_LOCK:
                        job["results"].append({
                            "index": idx,
                            "task_id": task_id,
                            "filename": f"output_{idx}.mp4",
                            "download_url": extract_video_url(task_result) or "",
                            "status": "succeeded",
                        })
                        job["done"] += 1
                break
            elif t_status in ("failed", "error"):
                with JOBS_LOCK:
                    job["errors"].append(f"Run {idx}: {t_status}")
                    job["done"] += 1
                break

    with JOBS_LOCK:
        job["status"] = "succeeded" if len(job.get("results", [])) > 0 else "failed"
        job["finished_at"] = time.time()
        job["events"].append({"time": time.strftime("%H:%M:%S"), "message": f"任务结束: {job['status']}"})
        final_snapshot = {
            "status": job["status"],
            "done": job.get("done", 0),
            "total": job.get("total", 0),
            "results": [{k: v for k, v in r.items()} for r in job.get("results", [])],
            "errors": list(job.get("errors", [])),
        }
    try:
        update_activity(job.get("activity_id"), status=final_snapshot["status"], result=final_snapshot,
                        error="; ".join(final_snapshot["errors"][:3]) if final_snapshot["errors"] else None)
    except Exception:
        pass


# === Real Portrait Handlers (delegate to unified handlers) ===
# Real-person assets use the same Asset API and video generation as virtual.
# Face verification is done on the Volcengine console, not via API.

def handle_real_assets_post(handler):
    handle_virtual_assets_post(handler)


def handle_real_assets_get(handler, asset_id=None):
    handle_virtual_assets_get(handler, asset_id)


def handle_real_assets_delete(handler, asset_id):
    handle_virtual_assets_delete(handler, asset_id)


def handle_real_jobs_post(handler):
    handle_virtual_jobs_post(handler, task_type="real")


def handle_real_jobs_get(handler, job_id=None):
    handle_virtual_jobs_get(handler, job_id)


def handle_real_group_get(handler, group_id):
    handle_virtual_group_get(handler, group_id)


def handle_real_group_update(handler, group_id):
    handle_virtual_group_update(handler, group_id)


def handle_real_asset_update(handler, asset_id):
    handle_virtual_asset_update(handler, asset_id)


def handle_real_group_delete(handler, group_id):
    handle_virtual_group_delete(handler, group_id)


def handle_real_groups_get(handler):
    handle_virtual_groups_get(handler)


# === HTTP Handler ===

class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        path = urllib.parse.urlparse(path).path
        if path.startswith("/outputs/"):
            return str((OUTPUT_DIR / path.removeprefix("/outputs/")).resolve())
        if path.startswith("/uploads/"):
            return str((UPLOAD_DIR / path.removeprefix("/uploads/")).resolve())
        if path in {"/", "/index.html"}:
            return str(STATIC_DIR / "index.html")
        return str((STATIC_DIR / path.lstrip("/")).resolve())

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/v1/meta":
            json_response(self, 200, {
                "app": "volcengine-portrait",
                "version": "1.0.0",
                "port": PORT,
                "capabilities": ["portrait-assets", "video-generation"],
                "status": "ready",
            })
            return
        if path == "/api/config":
            json_response(self, 200, {
                "ok": True,
                "base_url": ARK_BASE_URL,
                "has_key": bool(API_KEY),
                "has_aksk": bool(ACCESS_KEY and SECRET_KEY),
                "has_api_key": bool(API_KEY),
                "has_access_key": bool(ACCESS_KEY),
                "has_secret_key": bool(SECRET_KEY),
                "output_dir": str(OUTPUT_DIR),
            })
            return

        # Virtual portrait
        if path == "/api/virtual/groups":
            handle_virtual_groups_get(self)
            return
        if path.startswith("/api/virtual/groups/"):
            handle_virtual_group_get(self, path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/virtual/assets/"):
            handle_virtual_assets_get(self, path.rsplit("/", 1)[-1])
            return
        if path == "/api/virtual/assets":
            handle_virtual_assets_get(self)
            return
        if path.startswith("/api/virtual/jobs/"):
            handle_virtual_jobs_get(self, path.rsplit("/", 1)[-1])
            return
        if path == "/api/virtual/jobs":
            handle_virtual_jobs_get(self)
            return

        # Real portrait (same handlers as virtual)
        if path.startswith("/api/real/assets/"):
            handle_real_assets_get(self, path.rsplit("/", 1)[-1])
            return
        if path == "/api/real/assets":
            handle_real_assets_get(self)
            return
        if path.startswith("/api/real/jobs/"):
            handle_real_jobs_get(self, path.rsplit("/", 1)[-1])
            return
        if path == "/api/real/jobs":
            handle_real_jobs_get(self)
            return
        if path.startswith("/api/real/groups/"):
            handle_real_group_get(self, path.rsplit("/", 1)[-1])
            return
        if path == "/api/real/groups":
            handle_real_groups_get(self)
            return

        # Generic job lookup (for Portal UsageTracker polling)
        if path.startswith("/api/jobs/"):
            handle_virtual_jobs_get(self, path.rsplit("/", 1)[-1])
            return

        # Download / uploads
        # Activity log (portal aggregates this)
        if path == "/api/activity":
            sees_all, username = _view_scope(self)
            json_response(self, 200, activity_list(sees_all=sees_all, username=username))
            return
        if path.startswith("/api/activity/"):
            activity_id = path.rsplit("/", 1)[-1]
            record = next((item for item in read_activity_log() if item.get("id") == activity_id), None)
            json_response(self, 200 if record else 404,
                          activity_record_for_client(record) or {"ok": False, "error": "activity not found"})
            return

        if path.startswith("/api/download/"):
            token = path.rsplit("/", 1)[-1]
            with FILES_LOCK:
                fpath = FILES.get(token)
            if not fpath or not fpath.exists():
                json_response(self, 404, {"ok": False, "error": "file not found"})
                return
            self._serve_file(fpath)
            return
        if path.startswith("/uploads/"):
            fpath = UPLOAD_DIR / path.removeprefix("/uploads/")
            if fpath.exists():
                self._serve_file(fpath)
                return

        super().do_GET()

    def _serve_file(self, fpath):
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(fpath.name)[0] or "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{fpath.name}"')
        self.send_header("Content-Length", str(fpath.stat().st_size))
        if CORS:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(fpath.read_bytes())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            handle_config_post(self)
            return
        if path == "/api/choose-output-dir":
            client_ip = self.headers.get("X-Forwarded-For") or self.client_address[0]
            if client_ip not in ("127.0.0.1", "::1", "localhost"):
                json_response(self, 200, {"remote": True})
                return
            try:
                json_response(self, 200, {"path": choose_output_dir()})
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return
        if path == "/api/virtual/groups":
            handle_virtual_groups_post(self)
            return
        if path.startswith("/api/virtual/groups/"):
            handle_virtual_group_update(self, path.rsplit("/", 1)[-1])
            return
        if path == "/api/virtual/assets":
            handle_virtual_assets_post(self)
            return
        if path.startswith("/api/virtual/assets/"):
            handle_virtual_asset_update(self, path.rsplit("/", 1)[-1])
            return
        if path == "/api/virtual/jobs":
            handle_virtual_jobs_post(self)
            return
        if path == "/api/real/jobs":
            handle_real_jobs_post(self)
            return
        if path.startswith("/api/real/groups/"):
            handle_real_group_update(self, path.rsplit("/", 1)[-1])
            return
        if path == "/api/real/assets":
            handle_real_assets_post(self)
            return
        if path.startswith("/api/real/assets/"):
            handle_real_asset_update(self, path.rsplit("/", 1)[-1])
            return
        json_response(self, 404, {"ok": False, "error": "not found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/virtual/assets/"):
            handle_virtual_assets_delete(self, path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/virtual/groups/"):
            handle_virtual_group_delete(self, path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/real/assets/"):
            handle_real_assets_delete(self, path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/real/groups/"):
            handle_real_group_delete(self, path.rsplit("/", 1)[-1])
            return
        json_response(self, 404, {"ok": False, "error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        if CORS:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Workspace-Id, X-Api-Key, X-Access-Key, X-Secret-Key")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    load_config()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"  Volcengine Portrait → http://{HOST}:{PORT}")
    print(f"  Base URL: {ARK_BASE_URL}")
    print(f"  API Key: {'configured' if API_KEY else 'NOT configured'}")
    print(f"  AK/SK: {'configured' if ACCESS_KEY and SECRET_KEY else 'NOT configured'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
