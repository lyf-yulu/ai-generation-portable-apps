"""契约翻译层：画布 /api/v1/jobs ↔ nano-banana / seedance /api/jobs/json。

设计要点见 docs/infinite-canvas/03-契约翻译层.md。三条不可妥协的约束：

1. 结果必须落自己的盘。两个子应用的 download_url token 都是进程内存态
   （nano-banana 无持久化；seedance 的 load_files_map 只在 main() 里调用，
   而生产走 uvicorn 加载 app_fastapi，main() 永不执行）。而画布把结果地址
   存进项目文档 —— 直接存上游 token 链接，Portal 重启一次历史画布全裂。
2. 目录必须来自子应用的 /api/config，不能硬编码 —— 两个子应用的
   FALLBACK_PROVIDERS 与实际 providers.json 不一致。
3. 提交走 JSON 端点。只有 JSON 路径会合并 providers.json 默认值
   （nano-banana/app.py:1250-1258），multipart 路径落到硬编码字面量。
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import secrets
import ssl
import time
import urllib.parse
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import store

router = APIRouter()

APPS = {
    "nano-banana": int(os.environ.get("NANO_PORT", "8797")),
    "seedance": int(os.environ.get("SEEDANCE_PORT", "8787")),
}

# operation → (目标子应用, Portal 统计用的 task_type)
# task_type 决定统计按“张”还是按“秒”计（portal/app.py:1017-1022）：
# 含 "video"/"frame" 判 video，含 "text2image"/"image2image" 判 image。
OPERATION_ROUTING = {
    "image.generate": ("nano-banana", "text2image"),
    "image.edit": ("nano-banana", "image2image"),
    "video.generate": ("seedance", "text2video"),
    "video.image_to_video": ("seedance", "image2video"),
}

_CATALOG_TTL = 60
_catalog_cache: dict[str, tuple[float, list]] = {}

PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "9090"))
_PORTAL_SSL = ssl.create_default_context()
_PORTAL_SSL.check_hostname = False
_PORTAL_SSL.verify_mode = ssl.CERT_NONE


def _err(status: int, code: str, message: str, *, phase: str = "request",
         retryable: bool = False) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "code": code, "message": message, "retryable": retryable,
        "request_id": "canvas", "phase": phase})


# ------------------------------------------------------------------ catalog

def _image_schema(provider: str, cfg: dict) -> dict:
    """把 nano-banana 的 provider 配置翻成画布认识的 JSON Schema 子集。

    只支持 parameterControls 认识的字段（web/src/components/model-picker.tsx:31-56）：
    type/enum/default/minimum/maximum/title/description。enum 校验很严 ——
    default 必须是 enum 中的值，否则该控件被静默丢弃。
    """
    defaults = cfg.get("defaults") or {}
    sizes = cfg.get("image_size_options") or ["1K", "1.5K", "2K", "4K"]
    size_default = defaults.get("image_size", "2K")
    if size_default not in sizes:
        size_default = sizes[0]
    ratios = ["auto", "1:1", "16:9", "9:16", "4:3", "3:4", "21:9"]
    ratio_default = defaults.get("aspect_ratio", "auto")
    if ratio_default not in ratios:
        ratio_default = "auto"
    properties = {
        "aspect_ratio": {"type": "string", "enum": ratios, "default": ratio_default, "title": "画幅"},
        "image_size": {"type": "string", "enum": list(sizes), "default": size_default, "title": "尺寸"},
        "repeat_count": {"type": "integer", "minimum": 1, "maximum": 10,
                         "default": int(defaults.get("repeat_count") or 1), "title": "生成数量"},
    }
    if cfg.get("supports_seed") is not False:
        properties["seed"] = {"type": "integer", "minimum": 0, "maximum": 2147483647, "title": "随机种子"}
    return {"type": "object", "properties": properties, "required": []}


def _video_schema(provider_defaults: dict, model: dict) -> dict:
    defaults = {**provider_defaults, **(model.get("defaults") or {})}
    lo, hi = (model.get("duration_range") or [4, 15])[:2]
    duration_default = int(defaults.get("duration") or lo)
    duration_default = max(lo, min(hi, duration_default))
    resolutions = model.get("resolutions") or ["480p", "720p"]
    res_default = defaults.get("resolution", "720p")
    if res_default not in resolutions:
        res_default = resolutions[0]
    ratios = model.get("ratios") or ["16:9"]
    ratio_default = defaults.get("ratio", "16:9")
    if ratio_default not in ratios:
        ratio_default = ratios[0]
    return {
        "type": "object",
        "properties": {
            "duration": {"type": "integer", "minimum": int(lo), "maximum": int(hi),
                         "default": duration_default, "title": "时长（秒）"},
            "resolution": {"type": "string", "enum": list(resolutions),
                           "default": res_default, "title": "分辨率"},
            "ratio": {"type": "string", "enum": list(ratios), "default": ratio_default, "title": "画幅"},
            "generate_audio": {"type": "boolean", "default": False, "title": "生成音频"},
            "watermark": {"type": "boolean", "default": False, "title": "水印"},
        },
        "required": [],
    }


async def _fetch_catalog(app: str) -> list:
    cached = _catalog_cache.get(app)
    if cached and time.time() - cached[0] < _CATALOG_TTL:
        return cached[1]
    models: list = []
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://127.0.0.1:{APPS[app]}/api/config")
        payload = resp.json()
        providers = payload.get("providers") or {}
        for provider, cfg in providers.items():
            if not isinstance(cfg, dict) or not isinstance(cfg.get("models"), list):
                continue
            label = cfg.get("label") or provider
            for entry in cfg["models"]:
                model_id = entry.get("id")
                if not model_id:
                    continue
                # 三段式 id，翻译回去时直接拆 —— 不另建映射表，少一份会不同步的状态。
                composite = f"{app}:{provider}:{model_id}"
                display = entry.get("label") or model_id
                if app == "nano-banana":
                    models.append({
                        "model_id": composite,
                        "service_id": app,
                        "display_name": f"{display}（{label}）",
                        "operations": ["image.generate", "image.edit"],
                        "input_media": ["text", "image"],
                        "parameter_schema": _image_schema(provider, cfg),
                        "input_ports": [{
                            "port_id": "reference_images", "media_type": "image",
                            "min_items": 0,
                            "max_items": int(cfg.get("max_reference_images") or 14),
                        }],
                    })
                else:
                    models.append({
                        "model_id": composite,
                        "service_id": app,
                        "display_name": display,
                        "operations": ["video.generate", "video.image_to_video"],
                        "input_media": ["text", "image"],
                        "parameter_schema": _video_schema(cfg.get("defaults") or {}, entry),
                        "input_ports": [
                            {"port_id": "first_frame", "media_type": "image", "min_items": 0, "max_items": 1},
                            {"port_id": "reference_images", "media_type": "image", "min_items": 0, "max_items": 9},
                        ],
                    })
    except Exception:
        # 子应用没起来时返回空目录而不是 500 —— 画布仍可用来拖节点画草图。
        models = []
    _catalog_cache[app] = (time.time(), models)
    return models


@router.get("/api/v1/models")
async def api_models(request: Request):
    catalogs = await asyncio.gather(*(_fetch_catalog(app) for app in APPS))
    return {"models": [model for catalog in catalogs for model in catalog]}


# --------------------------------------------------------------- submission

def _data_url(path: str, mime: str) -> str:
    blob = Path(path).read_bytes()
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def _collect_asset_ids(payload: dict) -> tuple[list[str], dict[str, list[str]]]:
    asset_ids = [a for a in (payload.get("asset_ids") or []) if isinstance(a, str)]
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    clean = {k: [i for i in v if isinstance(i, str)]
             for k, v in inputs.items() if isinstance(v, list)}
    return asset_ids, clean


def _build_nano_payload(model: str, provider: str, operation: str, prompt: str,
                        params: dict, media: dict) -> dict:
    body = {
        "provider": provider,
        "model": model,
        "prompt": prompt,
        "mode": "img2img" if operation == "image.edit" else "text2img",
        "aspect_ratio": str(params.get("aspect_ratio") or "auto"),
        "image_size": str(params.get("image_size") or "2K"),
        "response_format": "url",
        # count = max(repeat_count, concurrency)（nano-banana/app.py:1927-1930），
        # 所以 concurrency 固定 1，数量只由 repeat_count 控制，避免意外多出图多计费。
        "repeat_count": int(params.get("repeat_count") or 1),
        "concurrency": 1,
    }
    if params.get("seed") not in (None, ""):
        body["seed"] = str(params["seed"])
    if media:
        body["media"] = media
    return body


def _build_seedance_payload(model: str, prompt: str, params: dict, media: dict) -> dict:
    body = {
        "model": model,
        "prompt": prompt,
        "duration": int(params.get("duration") or 5),
        "resolution": str(params.get("resolution") or "720p"),
        "ratio": str(params.get("ratio") or "16:9"),
        "generate_audio": "1" if params.get("generate_audio") else "0",
        "watermark": "1" if params.get("watermark") else "0",
        "repeat_count": 1,
        "concurrency": 1,
    }
    if params.get("seed") not in (None, ""):
        body["seed"] = str(params["seed"])
    if media:
        body["media"] = media
    return body


@router.post("/api/v1/jobs")
async def api_create_job(request: Request):
    user = request.state.user
    payload = await request.json()
    if not isinstance(payload, dict):
        return _err(400, "invalid_request", "请求体无效。")

    operation = str(payload.get("operation") or "")
    routing = OPERATION_ROUTING.get(operation)
    if routing is None:
        return _err(400, "invalid_request", "不支持的生成类型。")
    app, task_type = routing

    composite = str(payload.get("model_id") or "")
    parts = composite.split(":", 2)
    if len(parts) != 3 or parts[0] != app:
        return _err(400, "invalid_request", "模型与生成类型不匹配。")
    _, provider, model = parts

    prompt = str(payload.get("prompt") or "").strip()
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    idem = str(payload.get("idempotency_key") or "")

    # 幂等：同 key 重复提交返回已存在的任务，不重复下发（否则网络抖动一次就双倍计费）。
    existing = store.find_job_by_idempotency(user["user_id"], idem)
    if existing is not None:
        return _job_response(existing)

    asset_ids, inputs = _collect_asset_ids(payload)

    # seedance 的两条硬规则前置校验（seedance/app.py:1701-1705）：
    # 违反了在上游是“任务失败”，前置返回 400 体验好得多。
    if app == "seedance":
        has_frame = bool(inputs.get("first_frame") or inputs.get("last_frame"))
        has_ref = bool(inputs.get("reference_images") or inputs.get("reference_video")
                       or inputs.get("reference_audio") or asset_ids)
        if has_frame and has_ref:
            return _err(400, "invalid_request", "首尾帧与参考图不能同时使用，请二选一。")

    # 素材转 data_url 内联。不用 {"url": ...} —— 子应用会服务端拉取该地址，
    # 而我们的 assets 接口要求 Portal 签名头，它给不出；为此开免鉴权端点会凭空扩大攻击面。
    media: dict = {}
    try:
        if app == "nano-banana":
            ordered = inputs.get("reference_images") or asset_ids
            for index, asset_id in enumerate(ordered[:14], start=1):
                row = store.get_asset(user["user_id"], asset_id)
                media[f"image_{index}"] = {"data_url": _data_url(row["path"], row["mime_type"])}
        else:
            for slot, ids in (("first_frame", inputs.get("first_frame") or []),
                              ("last_frame", inputs.get("last_frame") or [])):
                if ids:
                    row = store.get_asset(user["user_id"], ids[0])
                    media[slot] = {"data_url": _data_url(row["path"], row["mime_type"])}
            refs = inputs.get("reference_images") or ([] if media else asset_ids)
            for index, asset_id in enumerate(refs[:9], start=1):
                row = store.get_asset(user["user_id"], asset_id)
                media[f"ref_image_{index}"] = {"data_url": _data_url(row["path"], row["mime_type"])}
    except store.NotFoundError:
        return _err(400, "invalid_request", "引用的素材不存在。")

    body = (_build_nano_payload(model, provider, operation, prompt, params, media)
            if app == "nano-banana"
            else _build_seedance_payload(model, prompt, params, media))

    # 子应用不要求鉴权，但要带 X-Username（决定其输出目录与任务归属）
    # 和 X-Workspace-Id（不给会落到 localhost 默认工作区，可能被历史预设注入参考图）。
    headers = {
        "Content-Type": "application/json",
        "X-Username": urllib.parse.quote(user["username"], safe=""),
        "X-Workspace-Id": "infinite-canvas",
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"http://127.0.0.1:{APPS[app]}/api/jobs/json",
                                     content=json.dumps(body), headers=headers)
        upstream = resp.json()
    except Exception:
        return _err(502, "upstream_unavailable", "生成服务暂时不可用。",
                    phase="submission", retryable=True)

    if resp.status_code != 200 or not upstream.get("ok"):
        message = str(upstream.get("error") or "生成服务拒绝了该请求。")[:160]
        return _err(400, "submission_rejected", message, phase="submission")

    job_id = secrets.token_urlsafe(12).replace("=", "")
    store.insert_job({
        "job_id": job_id,
        "user_id": user["user_id"],
        "username": user["username"],
        "app": app,
        "upstream_id": upstream.get("job_id"),
        "operation": operation,
        "task_type": task_type,
        "status": "running",
        "total": int(params.get("repeat_count") or 1),
        "duration": int(params.get("duration") or 0) if app == "seedance" else 0,
        "idempotency_key": idem,
    })
    return _job_response(store.get_job(job_id), created=True)


def _job_state(job: dict) -> dict:
    """画布 JobState 与 Portal 统计字段的并集。

    上半段给前端（web/src/api/contracts.ts:23），下半段给 Portal 轮询
    （portal/app.py:993-1037）。两边各读各的，互不干扰。
    """
    results = job.get("results") or []
    state = {
        "id": job["job_id"],
        "operation": job["operation"],
        "status": job["status"],
        "results": results,
        # ---- Portal 统计 ----
        "done": int(job.get("done") or 0),
        "total": int(job.get("total") or 0),
        "task_type": job["task_type"],
        "duration": int(job.get("duration") or 0),
    }
    if results:
        state["result_url"] = results[0]["url"]
    if job.get("error_message"):
        state["error"] = {"code": "generation_failed", "message": job["error_message"][:160],
                          "retryable": False, "request_id": job["job_id"], "phase": "generation"}
    return state


def _job_response(job: dict, *, created: bool = False) -> JSONResponse:
    # Portal 只在 200/201 且带非空 X-Job-Id 时登记统计（portal/app.py:2093-2097）。
    # 异步任务的直觉是返 202 —— 那样就完全不计数了。
    return JSONResponse(
        status_code=201 if created else 200,
        content=_job_state(job),
        headers={"X-Job-Id": job["job_id"], "Access-Control-Expose-Headers": "X-Job-Id"},
    )


# ----------------------------------------------------------------- polling

def _ingest_results(job: dict, upstream: dict) -> tuple[list, int]:
    """把上游结果的字节搬进我们自己的 assets，返回 (results, done)。

    优先读 local_path（同机、零 HTTP 开销、不受上游 token 生死影响），
    回退 download_url。两者都失败就不计入 done —— 不假装成功。
    """
    app = job["app"]
    user_id = job["user_id"]
    items: list[dict] = []
    for entry in upstream.get("results") or []:
        # nano-banana 嵌套在 images[]，seedance 平铺在 result 上 —— 这是两者最大的形状差异。
        candidates = entry.get("images") if app == "nano-banana" else [entry]
        for item in candidates or []:
            if not isinstance(item, dict):
                continue
            media_type = "image" if app == "nano-banana" else "video"
            blob = None
            local = item.get("local_path")
            if local and Path(local).is_file():
                blob = Path(local).read_bytes()
            if blob is None and item.get("download_url"):
                try:
                    with httpx.Client(timeout=60) as client:
                        resp = client.get(f"http://127.0.0.1:{APPS[app]}{item['download_url']}")
                    if resp.status_code == 200:
                        blob = resp.content
                except Exception:
                    blob = None
            if not blob:
                continue
            name = item.get("filename") or ""
            mime = mimetypes.guess_type(name)[0] or (
                "image/png" if media_type == "image" else "video/mp4")
            asset_id = secrets.token_urlsafe(16).replace("=", "")
            user_dir = store.UPLOAD_DIR / "".join(
                c if c.isalnum() or c in "_-" else "_" for c in user_id)[:64]
            user_dir.mkdir(parents=True, exist_ok=True)
            ext = Path(name).suffix or (".png" if media_type == "image" else ".mp4")
            path = user_dir / f"{asset_id}{ext}"
            path.write_bytes(blob)
            store.insert_asset(user_id, asset_id, media_type, mime, len(blob),
                               str(path), origin="generated")
            items.append({
                # 存裸路径：挂载前缀是部署决定，存进文档会把它焊进用户数据。
                "url": f"/api/v1/assets/{asset_id}/content",
                "asset_id": asset_id,
                "media_type": media_type,
            })
    return items, len(items)


async def _refresh(job: dict) -> dict:
    """向上游拉一次状态；进入终态时同步完成摄取再对外报 succeeded。"""
    if job["status"] in ("succeeded", "failed") or not job.get("upstream_id"):
        return job
    app = job["app"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"http://127.0.0.1:{APPS[app]}/api/jobs/{job['upstream_id']}")
    except Exception:
        return job
    if resp.status_code == 404:
        # 上游任务已被清理（nano-banana MAX_JOBS=500 / 完成 600s 后可能被剪），
        # 且我们没记到终态 —— 如实报失败，不无限重试。
        store.update_job(job["job_id"], status="failed", done=0,
                         error_message="上游任务状态已丢失。")
        return store.get_job(job["job_id"])
    if resp.status_code != 200:
        return job

    upstream = resp.json()
    status = str(upstream.get("status") or "")
    if status not in ("succeeded", "failed", "completed"):
        return job

    results, done = _ingest_results(job, upstream)
    duration = int(job.get("duration") or 0)
    if app == "seedance":
        for entry in upstream.get("results") or []:
            actual = entry.get("duration")
            if isinstance(actual, (int, float)) and actual > 0:
                duration = int(actual)
                break

    # done 是真实产出件数：纯失败报 0，部分成功报实际数（上游 3 成功 1 失败就报 3）。
    # 绝不 max(1,...) 兜底 —— portal/app.py:1003-1010 明确警告那会造幽灵计数。
    final = "succeeded" if done > 0 else "failed"
    error_message = None
    if done == 0:
        errors = upstream.get("errors") or []
        error_message = str(errors[0])[:160] if errors else "生成失败。"

    store.update_job(job["job_id"], status=final, done=done, duration=duration,
                     results=results, error_message=error_message)
    updated = store.get_job(job["job_id"])
    _report_final_to_portal(updated["job_id"], final)
    return updated


def _report_final_to_portal(job_id: str, status: str) -> None:
    """告知 Portal 任务已终态，失败时回滚 +1 的任务计数。

    照抄 nano-banana/app.py:214-235：Portal 是自签 HTTPS（关证书校验）、
    2 秒超时、异常全吞 —— 回调失败不能影响主流程，Portal 的轮询是兜底。
    """
    token = os.environ.get("PORTAL_INTERNAL_TOKEN", "")
    if not token or not job_id:
        return
    try:
        import urllib.request
        payload = json.dumps({"app": "infinite-canvas", "job_id": job_id, "status": status}).encode()
        req = urllib.request.Request(
            f"https://127.0.0.1:{PORTAL_PORT}/api/internal/jobs/finalize",
            data=payload,
            headers={"X-Internal-Token": token, "Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=2, context=_PORTAL_SSL).read()
    except Exception:
        pass


# 两个路由指向同一 handler：
#   /api/v1/jobs/{id}  → 画布前端
#   /api/jobs/{id}     → Portal 统计轮询（写死不带 /v1，portal/app.py:987）
@router.get("/api/v1/jobs/{job_id}")
@router.get("/api/jobs/{job_id}")
async def api_job_status(request: Request, job_id: str):
    job = store.get_job(job_id)
    if job is None:
        return _err(404, "not_found", "任务不存在。", phase="polling")
    job = await _refresh(job)
    return _job_state(job)


@router.post("/api/v1/jobs/{job_id}/cancel")
async def api_cancel_job(request: Request, job_id: str):
    job = store.get_job(job_id)
    if job is None:
        return _err(404, "not_found", "任务不存在。", phase="cancel")
    # 重要：这个路径是 POST 且命中 Portal 的 /api/v1/jobs 前缀白名单，
    # 响应绝不能带 X-Job-Id，否则每点一次取消统计就多记一个任务
    # （portal/app.py:2093-2097 的前两个条件它都满足）。
    return _job_state(job)
