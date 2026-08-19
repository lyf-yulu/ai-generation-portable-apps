"""Portal 签名身份校验。

画布挂在 Portal 的 /infinite-canvas/ 下，Portal 代理时不转发 Cookie
（portal/app.py:2015-2020 是请求头白名单），因此身份完全由签名头承载。

签名契约见 portal/app.py:129-136 _sign_admin_header：
    HMAC-SHA256(INTERNAL_TOKEN, f"{ts}:{'1' if is_admin else '0'}:{username}")
其中 username 是 **percent-encoded** 后的值（portal/app.py:2058-2060），
X-Portal-User-Id 不参与签名。

实现与 nano-banana/app.py:128-145 保持一致，便于对照维护。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse

# 与 nano-banana / seedance 一致：容忍 60 秒时钟偏差，防重放。
PORTAL_SIG_WINDOW = 60


def portal_token() -> str:
    """Portal 启动时生成并经 os.environ 注入子应用（portal/app.py:125-126, 613）。"""
    return os.environ.get("PORTAL_INTERNAL_TOKEN", "")


def verify_portal_identity(headers) -> dict | None:
    """校验 Portal 注入的签名身份。

    headers 需支持大小写不敏感的 .get()（Starlette 的 Headers 满足）。
    返回 {"user_id", "username", "role"}；任何一项校验失败返回 None，
    不区分失败原因（避免把校验细节透给调用方）。
    """
    token = portal_token()
    if not token:
        return None

    # 签名覆盖的是编码态的用户名，unquote 只用于对外展示 —— 顺序搞反必然验签失败。
    raw_username = headers.get("X-Username") or ""
    user_id = (headers.get("X-Portal-User-Id") or "").strip()
    is_admin = headers.get("X-Is-Admin") == "1"
    ts_raw = (headers.get("X-Portal-Ts") or "").strip()
    signature = (headers.get("X-Portal-Sig") or "").strip()

    if not raw_username or not user_id or not ts_raw.isdigit() or not signature:
        return None

    try:
        if abs(time.time() - int(ts_raw)) > PORTAL_SIG_WINDOW:
            return None
    except (TypeError, ValueError, OverflowError):
        return None

    message = f"{ts_raw}:{'1' if is_admin else '0'}:{raw_username}".encode("utf-8")
    expected = hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None

    try:
        username = urllib.parse.unquote(raw_username)
    except Exception:
        username = raw_username

    # Portal 的 viewer 角色没有 use_apps 权限、到不了这里（portal/app.py:1980），
    # 所以只需产出 admin / user 两种。
    return {
        "user_id": user_id,
        "username": username,
        "role": "admin" if is_admin else "user",
    }
