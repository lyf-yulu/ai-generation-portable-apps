from contextlib import asynccontextmanager, nullcontext
from collections.abc import Callable
from dataclasses import dataclass
from inspect import Parameter, signature
import logging
import os
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal
from urllib.parse import unquote_to_bytes

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError

from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.bitable.mvp_service import BitableMvpService
from feishu_generation_agent.bootstrap import (
    open_application_services,
    runtime_is_configured,
)
from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.asset_library import AssetKind
from feishu_generation_agent.domain.bitable import TableTaskStatus
from feishu_generation_agent.domain.document import (
    PlanningPromptSnapshot,
    build_planning_prompt_snapshot,
)
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.runtime import (
    GraphRuntime,
    RunConflict,
    RunNotFound,
    RunValidationError,
)
from feishu_generation_agent.integrations.bitable_delivery import (
    BitableResultConflict,
)
from feishu_generation_agent.integrations.feishu_bitable import BitableSchemaError
from feishu_generation_agent.integrations.planner import planner_system_prompt
from feishu_generation_agent.storage.bitable_tasks import TaskAlreadyClaimed
from feishu_generation_agent.storage.asset_library import (
    AssetLibraryStore,
    DuplicateAssetError,
)
from feishu_generation_agent.storage.production_tasks import ProductionTaskAlreadyClaimed
from feishu_generation_agent.storage.checkpoints import open_checkpointer
from feishu_generation_agent.storage.planner_prompts import PlannerPromptStore
from feishu_generation_agent.web.schemas import (
    AssetLibraryItem,
    AssetLibraryListResponse,
    AssetLibraryUpdateRequest,
    BitableClaimResponse,
    BitableRetryResponse,
    CreateRunRequest,
    DecisionRequest,
    PlannerPromptResponse,
    PlannerPromptUpdate,
    ReferenceListRequest,
)

ProductionCategory = Literal["animation", "portrait"]
_MAX_IDENTITY_LENGTH = 255
_LOGGER = logging.getLogger(__name__)
_WORKSPACE_STYLESHEET_LINK = (
    '<link rel="stylesheet" href="static/styles.css">'
)


def _render_workspace_html(static_dir: Path) -> str:
    html = (static_dir / "index.html").read_text("utf-8")
    if html.count(_WORKSPACE_STYLESHEET_LINK) != 1:
        raise RuntimeError(
            "workspace stylesheet link must appear exactly once"
        )
    styles = (static_dir / "styles.css").read_text("utf-8")
    inline = f"<style data-agent-inline-styles>\n{styles}\n</style>"
    return html.replace(_WORKSPACE_STYLESHEET_LINK, inline)


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    owner_user_id: str
    portal_user_id: str | None
    username: str
    is_portal: bool


def current_identity(request: Request) -> RequestIdentity:
    portal_user_id = request.headers.get("X-Portal-User-Id")
    if portal_user_id is None:
        return RequestIdentity(
            owner_user_id="prime-local",
            portal_user_id=None,
            username="",
            is_portal=False,
        )

    _validate_portal_user_id(portal_user_id)
    username = _decode_username(request.headers.get("X-Username", ""))
    return RequestIdentity(
        owner_user_id=portal_user_id,
        portal_user_id=portal_user_id,
        username=username,
        is_portal=True,
    )


def require_portal_identity(request: Request) -> RequestIdentity:
    identity = current_identity(request)
    if not identity.is_portal:
        raise HTTPException(status_code=403, detail="本地 Prime 提示词不可修改")
    return identity


def _validate_portal_user_id(value: str) -> None:
    if (
        not value
        or value == "prime-local"
        or value != value.strip()
        or len(value) > _MAX_IDENTITY_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise HTTPException(status_code=400, detail="Portal 用户身份无效")


def _decode_username(value: str) -> str:
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in "0123456789abcdefABCDEF"
            or value[index + 2] not in "0123456789abcdefABCDEF"
        ):
            raise HTTPException(status_code=400, detail="Portal 用户名编码无效")
    try:
        decoded = unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Portal 用户名编码无效") from exc
    if (
        len(decoded) > _MAX_IDENTITY_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
    ):
        raise HTTPException(status_code=400, detail="Portal 用户名无效")
    return decoded


def create_app(
    *,
    runtime: GraphRuntime | None = None,
    services: GraphServices | None = None,
    settings: Settings | None = None,
    bitable_service: BitableMvpService | Any | None = None,
    planner_prompt_store: PlannerPromptStore | None = None,
) -> FastAPI:
    if sum(value is not None for value in (runtime, services, settings)) > 1:
        raise ValueError("runtime, services and settings are mutually exclusive")
    static_dir = Path(__file__).with_name("static")

    @asynccontextmanager
    async def tracing_environment(settings):
        names = (
            "LANGSMITH_TRACING",
            "LANGCHAIN_TRACING_V2",
            "LANGSMITH_API_KEY",
            "LANGSMITH_PROJECT",
        )
        previous = {name: os.environ.get(name) for name in names}
        if settings.langsmith_tracing:
            settings.require("langsmith_api_key")
            os.environ["LANGSMITH_TRACING"] = "true"
            os.environ["LANGCHAIN_TRACING_V2"] = "true"
            os.environ["LANGSMITH_API_KEY"] = (
                settings.langsmith_api_key.get_secret_value()
            )
            os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        else:
            os.environ["LANGSMITH_TRACING"] = "false"
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
            os.environ.pop("LANGSMITH_API_KEY", None)
            os.environ.pop("LANGSMITH_PROJECT", None)
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    @asynccontextmanager
    async def activated_services(
        active_services: GraphServices,
        *,
        resume: bool = True,
    ) -> AsyncIterator[GraphRuntime]:
        async with open_checkpointer(active_services.settings) as checkpointer:
            active = GraphRuntime(
                graph=build_graph(active_services, checkpointer),
                repository=active_services.repository,
                file_store=active_services.file_store,
                settings=active_services.settings,
                delivery_writer=active_services.delivery_writer,
            )
            try:
                if resume:
                    await active.resume_pending_runs()
                yield active
            finally:
                await active.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if services is not None:
            async with tracing_environment(services.settings):
                async with activated_services(services) as active:
                    app.state.runtime = active
                    app.state.bitable_service = bitable_service
                    app.state.planner_prompt_store = planner_prompt_store
                    try:
                        yield
                    finally:
                        if bitable_service is not None:
                            await bitable_service.close()
                        app.state.bitable_service = None
                        app.state.planner_prompt_store = None
                        app.state.runtime = None
            return

        if runtime is not None:
            app.state.runtime = runtime
            app.state.bitable_service = bitable_service
            app.state.planner_prompt_store = planner_prompt_store
            try:
                yield
            finally:
                if bitable_service is not None:
                    await bitable_service.close()
                app.state.bitable_service = None
                app.state.planner_prompt_store = None
                await runtime.close()
                app.state.runtime = None
            return

        local_settings = settings or Settings()
        if runtime_is_configured(local_settings):
            async with tracing_environment(local_settings):
                async with open_application_services(local_settings) as application:
                    async with activated_services(
                        application.graph, resume=False
                    ) as active:
                        app.state.runtime = active
                        active_bitable = (
                            application.bitable_factory.create(active)
                            if application.bitable_factory is not None
                            else None
                        )
                        app.state.bitable_service = active_bitable
                        app.state.planner_prompt_store = (
                            planner_prompt_store
                            or getattr(application, "planner_prompt_store", None)
                        )
                        try:
                            if active_bitable is not None:
                                try:
                                    await active_bitable.resume_incomplete()
                                except Exception:
                                    # Keep the local UI available so a later scan can
                                    # report a safe, actionable readiness error.
                                    pass
                            else:
                                await active.resume_pending_runs()
                            yield
                        finally:
                            if active_bitable is not None:
                                await active_bitable.close()
                            app.state.bitable_service = None
                            app.state.planner_prompt_store = None
                            app.state.runtime = None
            return

        async with tracing_environment(local_settings):
            app.state.runtime = None
            app.state.bitable_service = None
            app.state.planner_prompt_store = planner_prompt_store
            try:
                yield
            finally:
                app.state.bitable_service = None
                app.state.planner_prompt_store = None
                app.state.runtime = None

    app = FastAPI(title="本地飞书生成任务 Agent", lifespan=lifespan)

    @app.middleware("http")
    async def prevent_static_asset_caching(request: Request, call_next):
        response = await call_next(request)
        # Portal forwards static assets after stripping its Agent mount prefix.
        if request.scope["path"].startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.get("/api/health")
    async def health() -> dict:
        active_settings = (
            services.settings
            if services is not None
            else runtime.settings
            if runtime is not None
            else settings or Settings()
        )

        def configured(*names: str) -> bool:
            for name in names:
                value = getattr(active_settings, name)
                if hasattr(value, "get_secret_value"):
                    value = value.get_secret_value()
                if value is None or (isinstance(value, str) and not value.strip()):
                    return False
            return True

        checks = {
            "local_storage": True,
            "feishu_read": configured("lark_app_id", "lark_app_secret"),
            "feishu_write": configured(
                "lark_app_id",
                "lark_app_secret",
                "lark_output_owner_open_id",
                "lark_output_folder_token",
            ),
            "bitable_read": configured(
                "lark_app_id",
                "lark_app_secret",
                "lark_bitable_url",
                "lark_bitable_table_id",
                "lark_bitable_view_id",
            ),
            "bitable_write": configured(
                "lark_app_id",
                "lark_app_secret",
                "lark_bitable_url",
                "lark_bitable_table_id",
                "lark_bitable_view_id",
            ),
            "planning": configured("deepseek_api_key", "deepseek_model"),
            "vision": configured("claude_api_key", "claude_model"),
            "image_generation": configured(
                "chiyun_api_key", "chiyun_model"
            ),
            "video_generation": configured("ark_api_key", "seedance_model"),
        }
        capabilities = {
            name: {
                "configured": value,
                "reachable": None,
                "permission_ok": None,
                "message": "已配置" if value else "缺少配置",
            }
            for name, value in checks.items()
        }
        return {
            "ready": (
                checks["local_storage"]
                and checks["feishu_read"]
                and checks["planning"]
                and checks["vision"]
                and checks["image_generation"]
                and checks["video_generation"]
                and (checks["bitable_write"] or checks["feishu_write"])
            ),
            "modes": {
                "bitable": checks["bitable_read"],
                "legacy_delivery": checks["feishu_write"],
            },
            "capabilities": capabilities,
        }

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

    def get_bitable_service(request: Request) -> Any:
        active = getattr(request.app.state, "bitable_service", None)
        if active is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="多维表格服务尚未配置",
            )
        return active

    def runtime_owner_scope(active: Any, owner_user_id: str):
        repository = getattr(active, "repository", None)
        owner_scope = getattr(repository, "owner_scope", None)
        if callable(owner_scope):
            return owner_scope(owner_user_id)
        return nullcontext()

    async def ensure_owned_run(
        active: Any, run_id: str, owner_user_id: str
    ) -> None:
        repository = getattr(active, "repository", None)
        get_run = getattr(repository, "get_run", None)
        if callable(get_run) and await get_run(
            run_id, owner_user_id=owner_user_id
        ) is None:
            raise RunNotFound("运行不存在")

    def owner_argument(callable_object: Any, owner_user_id: str) -> dict[str, str]:
        try:
            parameter = signature(callable_object).parameters.get(
                "owner_user_id"
            )
        except (TypeError, ValueError):
            parameter = None
        if parameter is None or parameter.kind is Parameter.POSITIONAL_ONLY:
            return {}
        return {"owner_user_id": owner_user_id}

    def planning_prompt_argument(
        callable_object: Any,
        planning_prompt: PlanningPromptSnapshot,
    ) -> dict[str, PlanningPromptSnapshot]:
        try:
            parameter = signature(callable_object).parameters.get(
                "planning_prompt"
            )
        except (TypeError, ValueError):
            parameter = None
        if parameter is None or parameter.kind is Parameter.POSITIONAL_ONLY:
            return {}
        return {"planning_prompt": planning_prompt}

    async def filter_bindings_by_runtime_owner(
        active_runtime: Any,
        bindings: list[Any],
        owner_user_id: str,
    ) -> list[Any]:
        repository = getattr(active_runtime, "repository", None)
        get_run = getattr(repository, "get_run", None)
        if not callable(get_run):
            return []
        visible: list[Any] = []
        for binding in bindings:
            if await get_run(
                binding.run_id, owner_user_id=owner_user_id
            ) is not None:
                visible.append(binding)
        return visible

    async def production_run_kind(
        active_bitable: Any,
        run_id: str,
        owner_user_id: str,
    ) -> bool | None:
        classifier = getattr(active_bitable, "is_production_run", None)
        if not callable(classifier):
            return None
        return await classifier(
            run_id,
            **owner_argument(classifier, owner_user_id),
        )

    def get_planner_prompt_store(request: Request) -> PlannerPromptStore:
        active = getattr(request.app.state, "planner_prompt_store", None)
        if active is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="提示词存储尚未配置",
            )
        return active

    def planner_prompt_response(
        identity: RequestIdentity,
        profile: Any | None = None,
    ) -> PlannerPromptResponse:
        if profile is not None:
            return PlannerPromptResponse(
                mode="personal",
                editable=True,
                prompt_text=profile.prompt_text,
                version=profile.version,
                source="personal",
            )
        return PlannerPromptResponse(
            mode="prime",
            editable=identity.is_portal,
            prompt_text=planner_system_prompt(),
            version=0,
            source="prime",
        )

    async def effective_planning_prompt(
        request: Request,
        identity: RequestIdentity,
    ) -> PlanningPromptSnapshot:
        profile = None
        if identity.is_portal:
            profile = await get_planner_prompt_store(request).get(
                identity.owner_user_id
            )
        snapshot = build_planning_prompt_snapshot(
            owner_user_id=identity.owner_user_id,
            source="personal" if profile is not None else "prime",
            version=profile.version if profile is not None else 0,
            prompt_text=(
                profile.prompt_text
                if profile is not None
                else planner_system_prompt()
            ),
        )
        _LOGGER.info(
            "Planning prompt snapshot owner_user_id=%s source=%s "
            "version=%d sha256=%s",
            snapshot.owner_user_id,
            snapshot.source,
            snapshot.version,
            snapshot.prompt_sha256,
        )
        return snapshot

    @app.get("/api/planner-prompt", response_model=PlannerPromptResponse)
    async def get_planner_prompt(request: Request) -> PlannerPromptResponse:
        identity = current_identity(request)
        if not identity.is_portal:
            return planner_prompt_response(identity)
        profile = await get_planner_prompt_store(request).get(
            identity.owner_user_id
        )
        return planner_prompt_response(identity, profile)

    @app.put("/api/planner-prompt", response_model=PlannerPromptResponse)
    async def update_planner_prompt(
        request: Request,
        identity: Annotated[RequestIdentity, Depends(require_portal_identity)],
        payload: PlannerPromptUpdate,
    ) -> PlannerPromptResponse:
        try:
            profile = await get_planner_prompt_store(request).save(
                portal_user_id=identity.owner_user_id,
                username=identity.username,
                prompt_text=payload.prompt_text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="提示词无效") from exc
        return planner_prompt_response(identity, profile)

    @app.delete("/api/planner-prompt", response_model=PlannerPromptResponse)
    async def delete_planner_prompt(request: Request) -> PlannerPromptResponse:
        identity = current_identity(request)
        if not identity.is_portal:
            raise HTTPException(status_code=403, detail="本地 Prime 提示词不可删除")
        await get_planner_prompt_store(request).delete(identity.owner_user_id)
        return planner_prompt_response(identity)

    def raise_bitable_error(exc: Exception) -> None:
        if (
            isinstance(exc, AgentError)
            and exc.detail.category is ErrorCategory.TRANSIENT
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="飞书服务暂时不可用，请稍后重试",
            ) from None
        if isinstance(exc, BitableSchemaError):
            raise HTTPException(
                status_code=422,
                detail="多维表格字段配置不兼容，请检查文本、需求来源、执行人和结果列",
            ) from None
        if isinstance(exc, RunValidationError):
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if isinstance(exc, BitableResultConflict):
            raise HTTPException(
                status_code=409,
                detail="结果列已有附件，已停止回写",
            ) from None
        if isinstance(exc, RunConflict) and str(exc).endswith("任务暂未启用"):
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if isinstance(exc, (TaskAlreadyClaimed, ProductionTaskAlreadyClaimed, RunConflict)):
            raise HTTPException(
                status_code=409,
                detail="该任务已被领取或当前不可处理",
            ) from None
        if isinstance(exc, RunNotFound):
            raise HTTPException(status_code=404, detail="多维表格运行不存在") from None
        raise HTTPException(
            status_code=502,
            detail="读取多维表格失败，请检查链接、权限和字段配置",
        ) from None

    @app.get("/api/bitable/tasks")
    async def scan_bitable_tasks(
        request: Request,
        category: ProductionCategory = "animation",
    ) -> list[dict]:
        active = get_bitable_service(request)
        try:
            tasks = await active.scan(category)
        except Exception as exc:
            raise_bitable_error(exc)
        return [_task_payload(task) for task in tasks]

    def _task_payload(task: Any) -> dict:
        if hasattr(task, "progress") and hasattr(task, "deliverable"):
            return {
                "record_id": task.record_id,
                "display_text": task.display_text,
                "source_url": task.source_url,
                "progress": task.progress,
                "task_type": task.task_type,
                "maker_name": task.maker_name,
                "deliverable": task.deliverable,
                "delivery_block_reason": task.delivery_block_reason,
            }
        return task.model_dump(mode="json")

    @app.get("/api/bitable/active-runs")
    async def list_active_bitable_runs(request: Request) -> list[dict]:
        active = get_bitable_service(request)
        identity = current_identity(request)
        try:
            ownership = owner_argument(
                active.active_runs, identity.owner_user_id
            )
            bindings = await active.active_runs(
                **ownership
            )
            if not ownership:
                bindings = await filter_bindings_by_runtime_owner(
                    get_runtime(request),
                    bindings,
                    identity.owner_user_id,
                )
        except Exception as exc:
            raise_bitable_error(exc)
        return [
            {
                "run_id": binding.run_id,
                "display_text": binding.display_text,
                "status": binding.status.value,
            }
            for binding in bindings
        ]

    @app.get("/api/bitable/recent-runs")
    async def list_recent_bitable_runs(request: Request) -> list[dict]:
        active = get_bitable_service(request)
        identity = current_identity(request)
        try:
            bindings = await active.recent_runs(
                **owner_argument(
                    active.recent_runs, identity.owner_user_id
                )
            )
        except Exception as exc:
            raise_bitable_error(exc)
        payload: list[dict] = []
        for binding in bindings:
            try:
                result_table_url = await active.result_table_url(
                    binding.run_id,
                    **owner_argument(
                        active.result_table_url,
                        identity.owner_user_id,
                    ),
                )
            except AttributeError:
                result_table_url = None
            payload.append(
                {
                    "run_id": binding.run_id,
                    "display_text": binding.display_text,
                    "status": binding.status.value,
                    "updated_at": binding.updated_at,
                    "result_table_url": result_table_url,
                    "rerunnable": binding.status in {
                        TableTaskStatus.COMPLETED,
                        TableTaskStatus.FAILED,
                    },
                }
            )
        return payload

    @app.post(
        "/api/bitable/tasks/{record_id}/claim",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def claim_bitable_task(
        record_id: str,
        request: Request,
        category: ProductionCategory = "animation",
    ) -> BitableClaimResponse:
        active = get_bitable_service(request)
        identity = current_identity(request)
        planning_prompt = await effective_planning_prompt(
            request, identity
        )
        try:
            runtime_for_owner = get_runtime(request)
            with runtime_owner_scope(
                runtime_for_owner, identity.owner_user_id
            ):
                run_id = await active.claim(
                    record_id,
                    category,
                    **owner_argument(active.claim, identity.owner_user_id),
                    **planning_prompt_argument(
                        active.claim, planning_prompt
                    ),
                )
        except Exception as exc:
            raise_bitable_error(exc)
        return BitableClaimResponse(run_id=run_id)

    @app.post(
        "/api/bitable/runs/{run_id}/retry-delivery",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_bitable_delivery(
        run_id: str, request: Request
    ) -> BitableRetryResponse:
        active = get_bitable_service(request)
        identity = current_identity(request)
        try:
            runtime_for_owner = get_runtime(request)
            await ensure_owned_run(
                runtime_for_owner, run_id, identity.owner_user_id
            )
            with runtime_owner_scope(
                runtime_for_owner, identity.owner_user_id
            ):
                await active.retry_delivery(
                    run_id,
                    **owner_argument(
                        active.retry_delivery, identity.owner_user_id
                    ),
                )
        except Exception as exc:
            raise_bitable_error(exc)
        return BitableRetryResponse(run_id=run_id)

    @app.post(
        "/api/bitable/runs/{run_id}/rerun",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def rerun_bitable_run(run_id: str, request: Request) -> BitableClaimResponse:
        active = get_bitable_service(request)
        identity = current_identity(request)
        try:
            runtime_for_owner = get_runtime(request)
            await ensure_owned_run(
                runtime_for_owner, run_id, identity.owner_user_id
            )
            with runtime_owner_scope(
                runtime_for_owner, identity.owner_user_id
            ):
                new_run_id = await active.rerun(
                    run_id,
                    **owner_argument(active.rerun, identity.owner_user_id),
                )
        except Exception as exc:
            raise_bitable_error(exc)
        return BitableClaimResponse(run_id=new_run_id)

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(payload: CreateRunRequest, request: Request) -> dict[str, str]:
        active = get_runtime(request)
        identity = current_identity(request)
        planning_prompt = await effective_planning_prompt(request, identity)
        domain_request = payload.to_domain().model_copy(
            update={"planning_prompt": planning_prompt}
        )
        with runtime_owner_scope(active, identity.owner_user_id):
            run_id = await active.start_run(domain_request)
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request):
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        active_bitable = getattr(request.app.state, "bitable_service", None)
        if active_bitable is not None:
            try:
                with runtime_owner_scope(
                    active, identity.owner_user_id
                ):
                    await active_bitable.sync_once(
                        run_id,
                        **owner_argument(
                            active_bitable.sync_once,
                            identity.owner_user_id,
                        ),
                    )
            except RunNotFound:
                pass
            except Exception as exc:
                raise_bitable_error(exc)
        try:
            with runtime_owner_scope(active, identity.owner_user_id):
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
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            active_bitable = getattr(request.app.state, "bitable_service", None)
            validate_approval = getattr(active_bitable, "validate_approval", None)
            production_kind = await production_run_kind(
                active_bitable, run_id, identity.owner_user_id
            )
            if (
                payload.action == "approve"
                and callable(validate_approval)
                and production_kind is not False
            ):
                await validate_approval(
                    run_id,
                    **owner_argument(
                        validate_approval, identity.owner_user_id
                    ),
                )
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.resume_run(run_id, payload.to_domain())
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except RunValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {"run_id": run_id, "status": "accepted"}

    @app.post(
        "/api/runs/{run_id}/retry-delivery",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_delivery(
        run_id: str, request: Request
    ) -> dict[str, str]:
        active = get_runtime(request)
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.retry_delivery(run_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"run_id": run_id, "status": "accepted"}

    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str, request: Request) -> dict[str, str]:
        active_bitable = getattr(request.app.state, "bitable_service", None)
        active = active_bitable or get_runtime(request)
        identity = current_identity(request)
        try:
            runtime_for_owner = get_runtime(request)
            await ensure_owned_run(
                runtime_for_owner, run_id, identity.owner_user_id
            )
            production_kind = await production_run_kind(
                active_bitable, run_id, identity.owner_user_id
            )
            if active_bitable is not None and production_kind is not False:
                with runtime_owner_scope(
                    runtime_for_owner, identity.owner_user_id
                ):
                    await active.delete_run(
                        run_id,
                        **owner_argument(
                            active.delete_run, identity.owner_user_id
                        ),
                    )
            else:
                with runtime_owner_scope(
                    runtime_for_owner, identity.owner_user_id
                ):
                    await runtime_for_owner.delete_run(run_id)
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return {"run_id": run_id, "status": "deleted"}

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
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            content = await file.read(active.settings.max_download_bytes + 1)
            if len(content) > active.settings.max_download_bytes:
                raise HTTPException(
                    status_code=422, detail="图片超过大小限制"
                )
            with runtime_owner_scope(active, identity.owner_user_id):
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
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                await active.set_references(
                    run_id,
                    task_id=task_id,
                    references=payload.references,
                    reference_mode=payload.reference_mode,
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
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
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
        identity = current_identity(request)
        try:
            await ensure_owned_run(active, run_id, identity.owner_user_id)
            with runtime_owner_scope(active, identity.owner_user_id):
                path, mime_type = await active.get_reference_file(
                    run_id, asset_id
                )
        except (RunNotFound, RunConflict, RunValidationError) as exc:
            raise_runtime_error(exc)
        return FileResponse(path, media_type=mime_type)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def workspace() -> HTMLResponse:
        return HTMLResponse(
            _render_workspace_html(static_dir),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    return app


def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def register_asset_library_routes(
    app: FastAPI,
    store_provider: Callable[[], AssetLibraryStore],
) -> None:
    @app.post(
        "/api/asset-library/assets",
        response_model=AssetLibraryItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_asset_library_item(
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form()],
        variant: Annotated[str, Form()] = "默认",
        kind: Annotated[str, Form()] = "character",
        description: Annotated[str, Form()] = "",
        aliases: Annotated[str | None, Form()] = None,
        tags: Annotated[str | None, Form()] = None,
        model_prefs: Annotated[str | None, Form()] = None,
        prompt_fragment: Annotated[str, Form()] = "",
    ) -> AssetLibraryItem:
        content = await file.read()
        try:
            asset = await store_provider().create(
                name=name,
                variant=variant,
                kind=AssetKind(kind),
                description=description,
                aliases=_split_csv(aliases),
                tags=_split_csv(tags),
                model_prefs=_split_csv(model_prefs),
                prompt_fragment=prompt_fragment,
                content=content,
                mime_type=file.content_type or "",
            )
        except DuplicateAssetError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        return AssetLibraryItem.from_domain(asset)

    @app.get(
        "/api/asset-library/assets",
        response_model=AssetLibraryListResponse,
    )
    async def list_asset_library_items(
        name: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> AssetLibraryListResponse:
        assets = await store_provider().list_all(
            kind=AssetKind(kind) if kind else None,
            name=name,
            limit=limit,
            offset=offset,
        )
        return AssetLibraryListResponse.from_domain(assets)

    @app.patch(
        "/api/asset-library/assets/{asset_id}",
        response_model=AssetLibraryItem,
    )
    async def update_asset_library_item(
        asset_id: str,
        payload: AssetLibraryUpdateRequest,
    ) -> AssetLibraryItem:
        try:
            asset = await store_provider().update(
                asset_id,
                name=payload.name,
                variant=payload.variant,
                kind=AssetKind(payload.kind) if payload.kind else None,
                description=payload.description,
                aliases=payload.aliases,
                tags=payload.tags,
                model_prefs=payload.model_prefs,
                prompt_fragment=payload.prompt_fragment,
            )
        except DuplicateAssetError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        if asset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="素材不存在"
            )
        return AssetLibraryItem.from_domain(asset)

    @app.delete(
        "/api/asset-library/assets/{asset_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_asset_library_item(asset_id: str) -> None:
        if not await store_provider().delete(asset_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="素材不存在"
            )
