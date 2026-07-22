"""Feishu (Lark) open-platform client — stdlib urllib only.

Scope of this client (intentionally small):
  - tenant_access_token with cached refresh + invalid-token retry
  - create a bitable App, create tables + fields
  - grant a user editor permission on the App (REQUIRED — an app-created base
    is owned by the app identity and is invisible to real users otherwise)
  - upload media (single-shot for small files, chunked for large videos)
  - append a record with an attachment

Everything is plain JSON POST / multipart. We deliberately do NOT depend on
httpx/pydantic so this ships with the same zero-build stdlib footprint as the
sub-apps. This module is fully independent from feishu-generation-agent/ and
.worktrees/feishu-bitable-bot/ (Codex); it only reuses the public API shapes.
"""
from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE_URL = "https://open.feishu.cn"
TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
TOKEN_INVALID_CODE = 99991663
# Feishu recommends chunked upload above ~20MB.
CHUNK_THRESHOLD = 20 * 1024 * 1024
CHUNK_SIZE = 4 * 1024 * 1024

# Field type ids per Feishu bitable spec: 1=text, 17=attachment.
_ATTACHMENT_FIELD = "结果"
_TABLE_FIELDS = [
    {"field_name": "文件名", "type": 1},
    {"field_name": "日期", "type": 1},
    {"field_name": "生成时间", "type": 1},
    {"field_name": "子应用", "type": 1},
    {"field_name": _ATTACHMENT_FIELD, "type": 17},
]

# app_name -> Chinese table name shown in the user's bitable.
APP_TABLE_NAMES = {
    "seedance": "Seedance(视频)",
    "nano-banana": "Nano Banana(图片)",
    "dreamina": "Dreamina",
    "volcengine-portrait": "人像生成",
}


class FeishuError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, *, folder_token: str = "",
                 opener=None):
        self._app_id = app_id
        self._app_secret = app_secret
        self._folder_token = folder_token
        self._token: str | None = None
        self._token_expiry = 0.0
        # opener injectable for tests (a callable(req)->file-like with .read()).
        self._opener = opener or urllib.request.urlopen

    # ---------------- low-level HTTP ----------------

    def _raw(self, method: str, path: str, *, headers: dict, data: bytes | None):
        req = urllib.request.Request(
            BASE_URL + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            raise FeishuError(
                f"飞书接口 HTTP {exc.code}: {method} {path}", detail=body[:500]
            ) from exc
        except urllib.error.URLError as exc:
            raise FeishuError(f"连接飞书失败: {method} {path} — {exc}") from exc

    def _json_call(self, method: str, path: str, body: dict | None = None) -> dict:
        """Authenticated JSON call with one auto-retry on token invalidation."""
        for attempt in range(2):
            token = self._tenant_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            }
            payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
            data = payload if method != "GET" else None
            result = self._raw(method, path, headers=headers, data=data)
            code = result.get("code", 0)
            if code == TOKEN_INVALID_CODE and attempt == 0:
                self._token = None  # force refresh and retry once
                continue
            if code != 0:
                raise FeishuError(
                    f"飞书接口返回错误 code={code}: {result.get('msg', '')}",
                    code=code, detail=json.dumps(result, ensure_ascii=False)[:500],
                )
            return result
        raise FeishuError("飞书接口重试后仍失败(token)")

    # ---------------- token ----------------

    def _tenant_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        headers = {"Content-Type": "application/json; charset=utf-8"}
        body = json.dumps(
            {"app_id": self._app_id, "app_secret": self._app_secret}
        ).encode("utf-8")
        result = self._raw("POST", TOKEN_PATH, headers=headers, data=body)
        if result.get("code", -1) != 0:
            raise FeishuError(
                f"获取 tenant_access_token 失败: {result.get('msg', '')}",
                code=result.get("code"),
            )
        token = result.get("tenant_access_token")
        expire = result.get("expire", 0)
        if not token:
            raise FeishuError("tenant_access_token 响应缺少 token")
        self._token = token
        # refresh 60s before actual expiry
        self._token_expiry = time.monotonic() + max(float(expire) - 60, 0)
        return token

    # ---------------- bitable app + tables ----------------

    def create_base_app(self, name: str) -> str:
        body: dict = {"name": name}
        if self._folder_token:
            body["folder_token"] = self._folder_token
        result = self._json_call("POST", "/open-apis/bitable/v1/apps", body)
        app_token = result.get("data", {}).get("app", {}).get("app_token")
        if not app_token:
            raise FeishuError("创建多维表格响应缺少 app_token")
        return app_token

    def create_table(self, app_token: str, table_name: str) -> str:
        body = {
            "table": {
                "name": table_name,
                "fields": _TABLE_FIELDS,
            }
        }
        result = self._json_call(
            "POST", f"/open-apis/bitable/v1/apps/{app_token}/tables", body
        )
        table_id = result.get("data", {}).get("table_id")
        if not table_id:
            raise FeishuError(f"创建数据表 {table_name} 响应缺少 table_id")
        return table_id

    def set_org_editable(self, app_token: str) -> None:
        """Open the base to everyone in the organization (link-share editable).

        Avoids per-user open_id mapping entirely: anyone in the tenant who has
        the link can view/edit. `type=bitable` MUST be a URL query param (it
        declares the token type) or the interface 400s with "type is required".
        `tenant_editable` = org members with the link can edit.
        """
        self._json_call(
            "PATCH",
            f"/open-apis/drive/v1/permissions/{app_token}/public?type=bitable",
            {"link_share_entity": "tenant_editable"},
        )

    def ensure_base_for_user(self, user: str,
                             registry) -> tuple[str, dict[str, str]]:
        """Return (app_token, {app: table_id}) for a user, creating on first use.

        The base is named by the user's login name (no open_id mapping needed)
        and opened to the whole organization, so any colleague with the link
        can view/edit. Idempotent via the registry: once created we reuse it,
        and if the org-share step previously failed we retry it without
        rebuilding the tables.
        """
        existing = registry.get_user_base(user)
        if existing:
            if not existing.get("authorized"):
                try:
                    self.set_org_editable(existing["app_token"])
                    registry.save_user_base(
                        user, existing["app_token"], existing["table_ids"],
                        authorized=True,
                    )
                except FeishuError:
                    pass  # retry next round; uploads can still proceed
            return existing["app_token"], existing["table_ids"]

        app_token = self.create_base_app(f"{user}的AI产出")
        table_ids: dict[str, str] = {}
        for app, table_name in APP_TABLE_NAMES.items():
            table_ids[app] = self.create_table(app_token, table_name)

        # Persist BEFORE the org-share call. If set_org_editable fails, the next
        # round must reuse this base instead of building a fresh one — otherwise
        # every retry leaks a new empty table. authorized=False flags that the
        # org-share still owes a retry.
        registry.save_user_base(user, app_token, table_ids, authorized=False)

        self.set_org_editable(app_token)
        registry.save_user_base(user, app_token, table_ids, authorized=True)
        return app_token, table_ids

    # ---------------- media upload ----------------

    def _multipart(self, path: str, fields: dict[str, str],
                   file_field: str, filename: str, blob: bytes,
                   mime: str) -> dict:
        boundary = f"----feishuoutputsync{uuid.uuid4().hex}"
        body = bytearray()
        for k, v in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            )
            body.extend(str(v).encode("utf-8"))
            body.extend(b"\r\n")
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (f'Content-Disposition: form-data; name="{file_field}"; '
             f'filename="{filename}"\r\n'
             f"Content-Type: {mime}\r\n\r\n").encode()
        )
        body.extend(blob)
        body.extend(f"\r\n--{boundary}--\r\n".encode())

        for attempt in range(2):
            token = self._tenant_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            }
            result = self._raw("POST", path, headers=headers, data=bytes(body))
            code = result.get("code", 0)
            if code == TOKEN_INVALID_CODE and attempt == 0:
                self._token = None
                continue
            if code != 0:
                raise FeishuError(
                    f"素材上传失败 code={code}: {result.get('msg', '')}",
                    code=code,
                )
            return result
        raise FeishuError("素材上传重试后仍失败(token)")

    def upload_media(self, app_token: str, file_path: Path) -> str:
        """Upload a media file to the bitable, return its file_token."""
        p = Path(file_path)
        size = p.stat().st_size
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if size <= CHUNK_THRESHOLD:
            return self._upload_all(app_token, p, size, mime)
        return self._upload_chunked(app_token, p, size, mime)

    def _upload_all(self, app_token: str, p: Path, size: int, mime: str) -> str:
        blob = p.read_bytes()
        result = self._multipart(
            "/open-apis/drive/v1/medias/upload_all",
            fields={
                "file_name": p.name,
                "parent_type": "bitable_file",
                "parent_node": app_token,
                "size": str(size),
            },
            file_field="file",
            filename=p.name,
            blob=blob,
            mime=mime,
        )
        token = result.get("data", {}).get("file_token")
        if not token:
            raise FeishuError("素材上传响应缺少 file_token")
        return token

    def _upload_chunked(self, app_token: str, p: Path, size: int,
                        mime: str) -> str:
        prep = self._json_call(
            "POST", "/open-apis/drive/v1/medias/upload_prepare",
            {
                "file_name": p.name,
                "parent_type": "bitable_file",
                "parent_node": app_token,
                "size": size,
            },
        )
        data = prep.get("data", {})
        upload_id = data.get("upload_id")
        block_size = int(data.get("block_size") or CHUNK_SIZE)
        if not upload_id:
            raise FeishuError("分片上传预处理缺少 upload_id")

        block_num = 0
        with p.open("rb") as fh:
            seq = 0
            while True:
                chunk = fh.read(block_size)
                if not chunk:
                    break
                self._multipart(
                    "/open-apis/drive/v1/medias/upload_part",
                    fields={
                        "upload_id": upload_id,
                        "seq": str(seq),
                        "size": str(len(chunk)),
                    },
                    file_field="file",
                    filename=f"part-{seq}",
                    blob=chunk,
                    mime="application/octet-stream",
                )
                seq += 1
                block_num += 1

        finish = self._json_call(
            "POST", "/open-apis/drive/v1/medias/upload_finish",
            {"upload_id": upload_id, "block_num": block_num},
        )
        token = finish.get("data", {}).get("file_token")
        if not token:
            raise FeishuError("分片上传完成响应缺少 file_token")
        return token

    # ---------------- records ----------------

    def add_record(self, app_token: str, table_id: str,
                   fields: dict[str, str], file_token: str) -> str:
        record_fields = dict(fields)
        record_fields[_ATTACHMENT_FIELD] = [{"file_token": file_token}]
        result = self._json_call(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            {"fields": record_fields},
        )
        record_id = result.get("data", {}).get("record", {}).get("record_id")
        if not record_id:
            raise FeishuError("新增记录响应缺少 record_id")
        return record_id
