#!/usr/bin/env python3
"""FastAPI port of nano-banana's HTTP layer.

Coexists with the stdlib app.py: business functions are imported from there,
only the HTTP dispatch is replaced. This lets us diff behaviour side-by-side
before committing to a full cutover.

Run with:
    DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
    .venv/bin/uvicorn nano-banana.app_fastapi:app --port 8799 --host 127.0.0.1

The DYLD var is needed on macOS to load Homebrew's expat; see requirements.txt.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Annotated

# Import the sibling stdlib app as a module. Business functions live there;
# we only replace the HTTP layer.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import app as legacy  # nano-banana/app.py

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))

app = FastAPI(
    title="nano-banana",
    version="2.0.0-fastapi-poc",
    docs_url=None,  # No auto-docs endpoint; behaviour parity with stdlib version
    redoc_url=None,
    openapi_url=None,
)


# ------------------------- middleware -------------------------------


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Enforces (a) upload size limit and (b) nosniff on every response.

    Mirrors what Handler did in the stdlib version — see stage 1c / 1d."""
    raw_cl = request.headers.get("content-length")
    if raw_cl:
        try:
            n = int(raw_cl)
        except (TypeError, ValueError):
            n = -1
        if n > MAX_UPLOAD_BYTES:
            return JSONResponse(
                status_code=413,
                content={
                    "ok": False,
                    "error": f"upload too large: {n} bytes (limit {MAX_UPLOAD_BYTES})",
                },
            )
    resp: Response = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# CORS mirrors stage 1e: whitelist derived from ALLOWED_ORIGINS or auto
# for LAN+loopback. Portal already handles CORS at the front; this middleware
# is only used when running nano-banana standalone.
_ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _ALLOWED_ORIGINS.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Workspace-Id", "X-Api-Key"],
    )


# ------------------------- helpers ----------------------------------


def _workspace_id(request: Request) -> str:
    """Match nano-banana's stdlib _workspace_id (?ws= or X-Workspace-Id or default)."""
    ws = (request.query_params.get("ws") or "").strip()
    if ws:
        return ws
    ws = (request.headers.get("X-Workspace-Id") or "").strip()
    return ws or "localhost"


# ------------------------- read-only endpoints ----------------------


@app.get("/api/v1/meta")
def api_meta():
    return {
        "app": "nano-banana",
        "version": "2.0.0-fastapi-poc",
        "port": int(os.environ.get("PORT", "8797")),
        "capabilities": ["text2image", "image2image"],
        "status": "ready",
    }


@app.get("/api/config")
def api_config():
    providers, config_error = legacy.load_provider_config()
    return {
        "ok": config_error is None,
        "providers": providers.get("providers", {}),
        "default_provider": providers.get("default_provider"),
        "config_error": config_error,
    }


@app.get("/api/request-template")
def api_request_template():
    return legacy.request_template()


@app.get("/api/schema")
def api_schema():
    return legacy.api_schema()


@app.get("/api/preset")
def api_preset_get(request: Request):
    return legacy.preset_for_client(_workspace_id(request))


@app.get("/api/archives")
def api_archives(request: Request):
    # legacy.list_archives accepts a handler for _decode_username; we don't
    # have one here. Pass None — it falls back to "localhost" scope, which
    # matches how the standalone (no-portal) frontend behaves.
    return {"archives": legacy.list_archives(None)}


@app.get("/api/activity")
def api_activity(request: Request):
    # _view_scope reads X-Is-Admin + verifies portal sig from headers.
    # We synthesize the tiny surface it needs (a handler-like with .headers).
    sees_all, username = legacy._view_scope(_HandlerShim(request))
    return legacy.activity_list(show_all=sees_all, username=username)


@app.get("/api/default-output-dir")
def api_default_output_dir():
    return {"path": legacy.desktop_output_dir()}


# ------------------------- handler shim -----------------------------


class _HandlerShim:
    """Minimal duck-type of SimpleHTTPRequestHandler for legacy helpers.

    Only .headers is exposed — legacy._view_scope, _decode_username,
    _verify_portal_sig etc. all read from it. If a helper needs more
    (rfile, client_address) it means we need to port that helper away
    from the handler dependency; keep the shim small on purpose."""

    def __init__(self, request: Request):
        # Starlette headers behave like a dict for .get()
        self.headers = request.headers


# ------------------------- health -----------------------------------


@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}
