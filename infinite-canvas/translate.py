"""契约翻译层：画布 /api/v1/jobs ↔ nano-banana / seedance /api/jobs/json。

见 docs/infinite-canvas/03-契约翻译层.md。
本文件当前为 02 册的骨架版：目录返回空、提交返回未实现，
但已固定 Portal 统计所需的接口形状与响应头，便于先跑通链路。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import store

router = APIRouter()

# operation → (目标子应用, Portal 统计用的 task_type)
# task_type 决定统计按“张”还是按“秒”计（portal/app.py:1017-1022）。
OPERATION_ROUTING = {
    "image.generate": ("nano-banana", "text2image"),
    "image.edit": ("nano-banana", "image2image"),
    "video.generate": ("seedance", "text2video"),
    "video.image_to_video": ("seedance", "image2video"),
}


@router.get("/api/v1/models")
async def api_models(request: Request):
    # 03 册：从 nano-banana:8797 / seedance:8787 的 /api/config 翻译真实目录。
    # 子应用未就绪时返回空目录而不是 500 —— 画布仍可用来拖节点画草图。
    return {"models": []}


def _job_state(job: dict) -> dict:
    """画布侧 JobState 与 Portal 统计字段的并集。

    上半段给前端（web/src/api/contracts.ts:23），下半段给 Portal 轮询
    （portal/app.py:993-1037）。两边各读各的，互不干扰。
    """
    results = job.get("results") or []
    return {
        "id": job["job_id"],
        "operation": job["operation"],
        "status": job["status"],
        "results": results,
        "result_url": results[0]["url"] if results else None,
        "error": ({"code": "generation_failed", "message": job["error_message"],
                   "retryable": False, "request_id": job["job_id"], "phase": "generation"}
                  if job.get("error_message") else None),
        # ---- Portal 统计 ----
        "done": int(job.get("done") or 0),
        "total": int(job.get("total") or 0),
        "task_type": job["task_type"],
        "duration": int(job.get("duration") or 0),
    }


@router.post("/api/v1/jobs")
async def api_create_job(request: Request):
    return JSONResponse(
        status_code=503,
        content={"code": "not_implemented", "message": "生成链路尚未接入。",
                 "retryable": False, "request_id": "canvas", "phase": "submission"},
    )


# 两个路由指向同一 handler：
#   /api/v1/jobs/{id}  → 画布前端
#   /api/jobs/{id}     → Portal 统计轮询（写死不带 /v1，portal/app.py:987）
@router.get("/api/v1/jobs/{job_id}")
@router.get("/api/jobs/{job_id}")
async def api_job_status(request: Request, job_id: str):
    job = store.get_job(job_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={"code": "not_found", "message": "任务不存在。",
                     "retryable": False, "request_id": "canvas", "phase": "polling"},
        )
    return _job_state(job)


@router.post("/api/v1/jobs/{job_id}/cancel")
async def api_cancel_job(request: Request, job_id: str):
    job = store.get_job(job_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={"code": "not_found", "message": "任务不存在。",
                     "retryable": False, "request_id": "canvas", "phase": "cancel"},
        )
    # 重要：这个路径是 POST 且命中 Portal 的 /api/v1/jobs 前缀白名单，
    # 响应绝不能带 X-Job-Id，否则每点一次取消统计就多记一个任务
    # （portal/app.py:2093-2097）。
    return _job_state(job)
