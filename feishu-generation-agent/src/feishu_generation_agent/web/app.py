from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError

from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.runtime import (
    GraphRuntime,
    RunConflict,
    RunNotFound,
    RunValidationError,
)
from feishu_generation_agent.storage.checkpoints import open_checkpointer
from feishu_generation_agent.web.schemas import (
    CreateRunRequest,
    DecisionRequest,
    ReferenceListRequest,
)


def create_app(
    *,
    runtime: GraphRuntime | None = None,
    services: GraphServices | None = None,
) -> FastAPI:
    if runtime is not None and services is not None:
        raise ValueError("runtime and services cannot both be provided")
    static_dir = Path(__file__).with_name("static")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if services is not None:
            async with open_checkpointer(services.settings) as checkpointer:
                active = GraphRuntime(
                    graph=build_graph(services, checkpointer),
                    repository=services.repository,
                    file_store=services.file_store,
                    settings=services.settings,
                )
                app.state.runtime = active
                try:
                    yield
                finally:
                    await active.close()
                    app.state.runtime = None
            return

        app.state.runtime = runtime
        try:
            yield
        finally:
            if runtime is not None:
                await runtime.close()
            app.state.runtime = None

    app = FastAPI(title="本地飞书生成任务 Agent", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request
        details = [
            {
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg", "输入无效"),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    def get_runtime(request: Request) -> GraphRuntime:
        active = getattr(request.app.state, "runtime", None)
        if active is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="运行时尚未配置",
            )
        return active

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(payload: CreateRunRequest, request: Request) -> dict[str, str]:
        active = get_runtime(request)
        run_id = await active.start_run(payload.to_domain())
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request):
        active = get_runtime(request)
        try:
            return await active.get_run_view(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post(
        "/api/runs/{run_id}/decision",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def decide_run(
        run_id: str,
        payload: DecisionRequest,
        request: Request,
    ) -> dict[str, str]:
        active = get_runtime(request)
        try:
            await active.resume_run(run_id, payload.to_domain())
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except RunValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {"run_id": run_id, "status": "accepted"}

    def raise_runtime_error(exc: Exception) -> None:
        if isinstance(exc, RunNotFound):
            raise HTTPException(status_code=404, detail=str(exc)) from None
        if isinstance(exc, RunConflict):
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if isinstance(exc, RunValidationError):
            raise HTTPException(status_code=422, detail=str(exc)) from None
        raise exc

    @app.post(
        "/api/runs/{run_id}/references",
        status_code=status.HTTP_201_CREATED,
    )
    async def add_reference(
        run_id: str,
        request: Request,
        file: UploadFile = File(...),
        task_id: str = Form(...),
        role: str = Form(...),
        order: int = Form(..., ge=1),
        replaces_asset_id: str | None = Form(default=None),
    ) -> dict:
        active = get_runtime(request)
        content = await file.read(active.settings.max_download_bytes + 1)
        if len(content) > active.settings.max_download_bytes:
            raise HTTPException(status_code=422, detail="图片超过大小限制")
        try:
            return await active.add_reference(
                run_id,
                task_id=task_id,
                role=role,
                order=order,
                filename=file.filename or "upload",
                content=content,
                replaces_asset_id=replaces_asset_id,
            )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        raise AssertionError("unreachable")

    @app.patch("/api/runs/{run_id}/tasks/{task_id}/references")
    async def update_references(
        run_id: str,
        task_id: str,
        payload: ReferenceListRequest,
        request: Request,
    ) -> dict[str, str]:
        active = get_runtime(request)
        try:
            await active.set_references(
                run_id,
                task_id=task_id,
                references=payload.references,
            )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"status": "updated"}

    @app.delete("/api/runs/{run_id}/tasks/{task_id}/references/{asset_id}")
    async def unlink_reference(
        run_id: str,
        task_id: str,
        asset_id: str,
        request: Request,
    ) -> dict[str, str]:
        active = get_runtime(request)
        try:
            await active.unlink_reference(
                run_id,
                task_id=task_id,
                asset_id=asset_id,
            )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"status": "unlinked"}

    @app.get("/api/runs/{run_id}/references/{asset_id}/content")
    async def reference_content(
        run_id: str,
        asset_id: str,
        request: Request,
    ) -> FileResponse:
        active = get_runtime(request)
        try:
            path, mime_type = await active.get_reference_file(run_id, asset_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return FileResponse(path, media_type=mime_type)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def workspace() -> FileResponse:
        return FileResponse(static_dir / "index.html", media_type="text/html")

    return app
