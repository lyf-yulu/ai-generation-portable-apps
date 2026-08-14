import { useEffect, useMemo, useRef, useState } from "react";

import {
    changeAdminLogicalModelLifecycle,
    changeAdminModelRouteLifecycle,
    createAdminLogicalModel,
    createAdminModelRoute,
    deleteAdminLogicalModel,
    deleteAdminModelRoute,
    fetchAdminCredentialPools,
    fetchAdminLogicalModel,
    fetchAdminLogicalModels,
    fetchAdminModelRoute,
    fetchAdminModelRoutes,
    fetchAdminModels,
    fetchAdminUsers,
    replaceAdminUserModels,
    updateAdminLogicalModel,
    updateAdminModelRoute,
    type AdminCredentialPool,
    type AdminLogicalModel,
    type AdminModelRoute,
    type AdminUser,
} from "@/api/admin";
import type { ModelSpec } from "@/api/contracts";
import { ModelEditor } from "@/components/admin/model-editor";
import { CredentialPoolImport } from "@/components/admin/credential-pool-import";
import { ModelCallSettings } from "@/components/admin/model-call-settings";
import { callingPresetsForModel, routeMatchesCallingPreset } from "@/components/admin/model-templates";
import { ObjectLifecycleActions } from "@/components/admin/object-lifecycle-actions";

export default function AdminModelsPage() {
    const [users, setUsers] = useState<AdminUser[]>([]);
    const [assignable, setAssignable] = useState<ModelSpec[]>([]);
    const [models, setModels] = useState<AdminLogicalModel[]>([]);
    const [pools, setPools] = useState<AdminCredentialPool[]>([]);
    const [routes, setRoutes] = useState<AdminModelRoute[]>([]);
    const [selectedModelId, setSelectedModelId] = useState("");
    const [selectedRouteId, setSelectedRouteId] = useState("");
    const [creatingModel, setCreatingModel] = useState(false);
    const [showArchived, setShowArchived] = useState(false);
    const [userId, setUserId] = useState("");
    const [assignedIds, setAssignedIds] = useState<string[]>([]);
    const [status, setStatus] = useState<"loading" | "ready" | "saving" | "saved" | "failed">("loading");
    const loadVersion = useRef(0);
    const routeVersion = useRef(0);
    const selectedModelIdRef = useRef("");
    const selectedRouteIdRef = useRef("");
    const selectedModel = useMemo(() => models.find((item) => item.model_id === selectedModelId) || null, [models, selectedModelId]);
    const selectedRoute = useMemo(() => routes.find((item) => item.route_id === selectedRouteId) || null, [routes, selectedRouteId]);
    const historicalModel = Boolean(selectedModel && !selectedModel.operation_contracts);
    const historicalRoute = Boolean(selectedRoute && !selectedRoute.operation_contracts);
    const user = useMemo(() => users.find((item) => item.user_id === userId), [users, userId]);
    useEffect(() => {
        selectedModelIdRef.current = selectedModelId;
    }, [selectedModelId]);
    useEffect(() => {
        selectedRouteIdRef.current = selectedRouteId;
    }, [selectedRouteId]);

    const load = async () => {
        const version = ++loadVersion.current;
        setStatus("loading");
        try {
            const [nextUsers, nextAssignable, nextModels, nextPools] = await Promise.all([fetchAdminUsers(), fetchAdminModels(), fetchAdminLogicalModels(showArchived), fetchAdminCredentialPools()]);
            if (version !== loadVersion.current) return;
            setUsers(nextUsers);
            setAssignable(nextAssignable);
            setModels(nextModels);
            setPools(nextPools);
            setSelectedModelId((current) => (nextModels.some((item) => item.model_id === current) ? current : nextModels[0]?.model_id || ""));
            setStatus("ready");
        } catch {
            if (version === loadVersion.current) setStatus("failed");
        }
    };
    useEffect(() => {
        void load();
        return () => {
            loadVersion.current += 1;
            routeVersion.current += 1;
        };
    }, [showArchived]);
    useEffect(() => {
        const version = ++routeVersion.current;
        setSelectedRouteId("");
        setRoutes([]);
        if (!selectedModelId) return;
        void fetchAdminModelRoutes(selectedModelId, showArchived)
            .then((next) => {
                if (routeVersion.current !== version) return;
                setRoutes(next);
                setSelectedRouteId(next[0]?.route_id || "");
            })
            .catch(() => {
                if (routeVersion.current === version) setStatus("failed");
            });
    }, [selectedModelId, showArchived]);
    useEffect(() => {
        setAssignedIds(user?.model_ids || []);
        setStatus((current) => (current === "loading" ? current : "ready"));
    }, [userId]);

    const replaceModel = (updated: AdminLogicalModel) => {
        setModels((current) => (current.some((item) => item.model_id === updated.model_id) ? current.map((item) => (item.model_id === updated.model_id ? updated : item)) : [...current, updated]));
        setSelectedModelId(updated.model_id);
        setCreatingModel(false);
    };
    const replaceRoute = (updated: AdminModelRoute) => {
        setRoutes((current) => (current.some((item) => item.route_id === updated.route_id) ? current.map((item) => (item.route_id === updated.route_id ? updated : item)) : [...current, updated]));
        setSelectedRouteId(updated.route_id);
    };
    const refreshModel = async () => {
        const modelId = selectedModelId;
        if (!modelId) return;
        const updated = await fetchAdminLogicalModel(modelId);
        if (selectedModelIdRef.current !== modelId) return;
        setModels((current) => current.map((item) => (item.model_id === updated.model_id ? updated : item)));
    };
    const refreshRoute = async () => {
        const modelId = selectedModelId,
            routeId = selectedRouteId;
        if (!modelId || !routeId) return;
        const updated = await fetchAdminModelRoute(modelId, routeId);
        if (selectedModelIdRef.current !== modelId || selectedRouteIdRef.current !== routeId) return;
        setRoutes((current) => current.map((item) => (item.route_id === updated.route_id ? updated : item)));
    };
    const refreshRoutes = async () => {
        const modelId = selectedModelId;
        if (!modelId) return;
        const version = ++routeVersion.current;
        try {
            const updated = await fetchAdminModelRoutes(modelId, showArchived);
            if (routeVersion.current !== version || selectedModelIdRef.current !== modelId) return;
            setRoutes(updated);
            setSelectedRouteId((current) => (updated.some((item) => item.route_id === current) ? current : updated[0]?.route_id || ""));
        } catch {
            if (routeVersion.current === version && selectedModelIdRef.current === modelId) setStatus("failed");
        }
    };
    const removeModel = () => {
        setModels((current) => {
            const next = current.filter((item) => item.model_id !== selectedModelId);
            setSelectedModelId(next[0]?.model_id || "");
            return next;
        });
    };
    const removeRoute = () => {
        setRoutes((current) => {
            const next = current.filter((item) => item.route_id !== selectedRouteId);
            setSelectedRouteId(next[0]?.route_id || "");
            return next;
        });
    };
    const toggleAssignment = (id: string) => setAssignedIds((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
    const saveAssignments = async () => {
        if (!userId || status === "saving") return;
        setStatus("saving");
        try {
            const response = await replaceAdminUserModels(userId, assignedIds);
            setUsers((current) => current.map((item) => (item.user_id === userId ? { ...item, model_ids: response.model_ids } : item)));
            setStatus("saved");
        } catch {
            setStatus("failed");
        }
    };
    const unavailableAssigned = assignedIds.filter((id) => !assignable.some((model) => model.model_id === id));

    return (
        <section className="mx-auto min-w-0 max-w-7xl overflow-x-clip px-4 py-7 text-[#e5f5e9] sm:px-5">
            <p className="text-xs tracking-[0.2em] text-[#58ed87]">ADMIN · LOGICAL MODELS</p>
            <div className="mt-2 flex flex-wrap items-end justify-between gap-3">
                <div>
                    <h1 className="text-2xl font-semibold sm:text-3xl">模型与调用线路</h1>
                    <p className="mt-2 max-w-3xl text-sm text-[#95ad9c]">用户只选择逻辑模型。Provider、线路和凭据池由管理员在这里隔离管理，真实凭据仅由部署配置提供。</p>
                </div>
                <label className="flex items-center gap-2 text-sm text-[#b9d0c0]">
                    <input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} className="accent-[#58ed87]" />
                    显示已归档
                </label>
            </div>
            {status === "loading" && (
                <p role="status" className="mt-5 text-sm text-[#86a991]">
                    正在加载管理配置…
                </p>
            )}
            {status === "failed" && (
                <p role="alert" className="mt-5 text-sm text-[#ffbd73]">
                    管理配置未能加载，请重试。
                </p>
            )}

            <CredentialPoolImport onImported={setPools} />

            <div className="mt-6 grid min-w-0 gap-5 lg:grid-cols-[17rem_minmax(0,1fr)]">
                <aside className="min-w-0 rounded-xl border border-[#245a35] bg-[#07110b] p-3">
                    <div className="flex items-center justify-between gap-2">
                        <h2 className="font-semibold">逻辑模型</h2>
                        <button
                            type="button"
                            onClick={() => {
                                setCreatingModel(true);
                                setSelectedModelId("");
                            }}
                            className="rounded bg-[#183f26] px-2.5 py-1.5 text-xs text-[#8ff0aa]"
                        >
                            新建
                        </button>
                    </div>
                    <div className="mt-3 grid gap-2" role="list" aria-label="逻辑模型列表">
                        {models.map((model) => (
                            <button
                                key={model.model_id}
                                type="button"
                                role="listitem"
                                onClick={() => {
                                    setCreatingModel(false);
                                    setSelectedModelId(model.model_id);
                                }}
                                className={`min-w-0 rounded-lg border p-3 text-left ${selectedModelId === model.model_id ? "border-[#58ed87] bg-[#102719]" : "border-[#1e482b] bg-[#0a1710]"}`}
                            >
                                <span className="block truncate text-sm font-medium">{model.display_name}</span>
                                <span className="mt-1 block truncate text-xs text-[#86a991]">
                                    {model.modality === "image" ? "图像" : "视频"} · {model.enabled ? "已启用" : "已停用"}
                                    {model.archived_at ? " · 已归档" : ""}
                                </span>
                            </button>
                        ))}
                    </div>
                </aside>
                <main className="min-w-0 space-y-5">
                    {creatingModel && <ModelEditor model={null} onSave={createAdminLogicalModel} onSaved={replaceModel} />}
                    {selectedModel && historicalModel && (
                        <>
                            <section className="rounded-xl border border-[#594d2a] bg-[#171408] p-4">
                                <h2 className="text-lg font-semibold text-[#eadc91]">只读历史模型</h2>
                                <p className="mt-2 text-sm text-[#cdbf83]">
                                    {selectedModel.display_name} · {selectedModel.model_id} · 修订 {selectedModel.revision}
                                </p>
                                <p className="mt-1 text-xs text-[#9e966d]">运行配置已清理，仅保留不可执行的审计信息。</p>
                            </section>
                            <ObjectLifecycleActions
                                historical
                                objectIdentity={selectedModel.model_id}
                                objectLabel={selectedModel.display_name}
                                enabled={false}
                                archivedAt={selectedModel.archived_at}
                                revision={selectedModel.revision}
                                onEnable={(revision) => changeAdminLogicalModelLifecycle(selectedModel.model_id, "enable", revision)}
                                onDisable={(revision) => changeAdminLogicalModelLifecycle(selectedModel.model_id, "disable", revision)}
                                onArchive={(revision) => changeAdminLogicalModelLifecycle(selectedModel.model_id, "archive", revision)}
                                onRestore={(revision) => changeAdminLogicalModelLifecycle(selectedModel.model_id, "restore", revision)}
                                onDelete={(revision) => deleteAdminLogicalModel(selectedModel.model_id, revision)}
                                onPurge={(revision) => changeAdminLogicalModelLifecycle(selectedModel.model_id, "purge-runtime", revision)}
                                onChanged={replaceModel}
                                onDeleted={removeModel}
                            />
                            <section className="min-w-0 rounded-xl border border-[#245a35] bg-[#07110b] p-4">
                                <h2 className="text-lg font-semibold">历史线路</h2>
                                <div className="mt-4 grid min-w-0 gap-2 sm:grid-cols-2">
                                    {routes.map((route) => (
                                        <button
                                            key={route.route_id}
                                            type="button"
                                            onClick={() => setSelectedRouteId(route.route_id)}
                                            className={`min-w-0 rounded-lg border p-3 text-left ${selectedRouteId === route.route_id ? "border-[#58ed87] bg-[#102719]" : "border-[#1e482b] bg-[#0a1710]"}`}
                                        >
                                            {route.route_id}
                                        </button>
                                    ))}
                                </div>
                            </section>
                            {selectedRoute && historicalRoute && (
                                <>
                                    <section className="rounded-xl border border-[#594d2a] bg-[#171408] p-4">
                                        <h2 className="text-lg font-semibold text-[#eadc91]">只读历史线路</h2>
                                        <p className="mt-2 text-sm text-[#cdbf83]">
                                            {selectedRoute.route_id} · 修订 {selectedRoute.revision}
                                        </p>
                                    </section>
                                    <ObjectLifecycleActions
                                        historical
                                        objectIdentity={`${selectedModel.model_id}:${selectedRoute.route_id}`}
                                        objectLabel={selectedRoute.route_id}
                                        enabled={false}
                                        archivedAt={selectedRoute.archived_at}
                                        revision={selectedRoute.revision}
                                        onEnable={(revision) => changeAdminModelRouteLifecycle(selectedModel.model_id, selectedRoute.route_id, "enable", revision)}
                                        onDisable={(revision) => changeAdminModelRouteLifecycle(selectedModel.model_id, selectedRoute.route_id, "disable", revision)}
                                        onArchive={(revision) => changeAdminModelRouteLifecycle(selectedModel.model_id, selectedRoute.route_id, "archive", revision)}
                                        onRestore={(revision) => changeAdminModelRouteLifecycle(selectedModel.model_id, selectedRoute.route_id, "restore", revision)}
                                        onDelete={(revision) => deleteAdminModelRoute(selectedModel.model_id, selectedRoute.route_id, revision)}
                                        onPurge={(revision) => changeAdminModelRouteLifecycle(selectedModel.model_id, selectedRoute.route_id, "purge-runtime", revision)}
                                        onChanged={replaceRoute}
                                        onDeleted={removeRoute}
                                    />
                                </>
                            )}
                        </>
                    )}
                    {selectedModel && !historicalModel && (
                        <>
                            <ModelEditor model={selectedModel} onSave={(body) => updateAdminLogicalModel(body as Parameters<typeof updateAdminLogicalModel>[0])} onSaved={replaceModel} onRefresh={() => void refreshModel()} />
                            <ObjectLifecycleActions
                                objectIdentity={selectedModel.model_id}
                                objectLabel={selectedModel.display_name}
                                enabled={selectedModel.enabled}
                                archivedAt={selectedModel.archived_at}
                                revision={selectedModel.revision}
                                onEnable={(revision) => changeAdminLogicalModelLifecycle(selectedModel.model_id, "enable", revision)}
                                onDisable={(revision) => changeAdminLogicalModelLifecycle(selectedModel.model_id, "disable", revision)}
                                onArchive={(revision) => changeAdminLogicalModelLifecycle(selectedModel.model_id, "archive", revision)}
                                onRestore={(revision) => changeAdminLogicalModelLifecycle(selectedModel.model_id, "restore", revision)}
                                onDelete={(revision) => deleteAdminLogicalModel(selectedModel.model_id, revision)}
                                onPurge={(revision) => changeAdminLogicalModelLifecycle(selectedModel.model_id, "purge-runtime", revision)}
                                onChanged={(updated) => {
                                    if (!showArchived && updated.archived_at) removeModel();
                                    else replaceModel(updated);
                                }}
                                onDeleted={removeModel}
                                onRefresh={() => void refreshModel()}
                            />
                            <ModelCallSettings
                                model={selectedModel}
                                routes={routes}
                                pools={pools}
                                onCreate={createAdminModelRoute}
                                onUpdate={(body) => updateAdminModelRoute(body)}
                                onLifecycle={(route, action) => changeAdminModelRouteLifecycle(selectedModel.model_id, route.route_id, action, route.revision)}
                                onSaved={replaceRoute}
                                onRefresh={() => void refreshRoutes()}
                            />
                            {(() => {
                                const presets = callingPresetsForModel(selectedModel);
                                const duplicateRouteIds = new Set(
                                    presets.flatMap((preset) => {
                                        const matching = routes.filter((route) => routeMatchesCallingPreset(route, preset));
                                        return matching.length > 1 ? matching.map((route) => route.route_id) : [];
                                    }),
                                );
                                const auditRoutes = routes.filter((route) => Boolean(route.archived_at) || duplicateRouteIds.has(route.route_id) || !presets.some((preset) => routeMatchesCallingPreset(route, preset)));
                                return (
                                    auditRoutes.length > 0 && (
                                        <details className="min-w-0 rounded-xl border border-[#594d2a] bg-[#171408] p-4">
                                            <summary className="cursor-pointer text-sm font-semibold text-[#eadc91]">历史审计记录（{auditRoutes.length}）</summary>
                                            <p className="mt-2 text-xs text-[#cdbf83]">这些记录已归档、重复、运行配置已清理，或不属于当前受信预置；仅保留只读追溯。</p>
                                            <ul className="mt-3 grid gap-2 text-sm text-[#cdbf83]">
                                                {auditRoutes.map((route) => (
                                                    <li key={route.route_id} className="rounded border border-[#594d2a] px-3 py-2">
                                                        <span className="font-mono">{route.route_id}</span> · 修订 {route.revision}
                                                    </li>
                                                ))}
                                            </ul>
                                        </details>
                                    )
                                );
                            })()}
                        </>
                    )}
                </main>
            </div>

            <section className="mt-8 min-w-0 rounded-xl border border-[#245a35] bg-[#07110b] p-4">
                <h2 className="text-lg font-semibold">用户模型派发</h2>
                <p className="mt-1 text-xs text-[#86a991]">只派发逻辑模型 ID；线路和凭据池对普通用户不可见。</p>
                <label className="mt-4 block max-w-md text-sm">
                    选择账号
                    <select aria-label="选择账号" value={userId} onChange={(event) => setUserId(event.target.value)} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2">
                        <option value="">请选择账号</option>
                        {users.map((item) => (
                            <option key={item.user_id} value={item.user_id}>
                                {item.display_name} · {item.username}
                            </option>
                        ))}
                    </select>
                </label>
                <div className="mt-4 grid min-w-0 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                    {assignable.map((model) => (
                        <label key={model.model_id} className="flex min-w-0 gap-2 rounded-lg border border-[#1e482b] bg-[#0a1710] p-3 text-sm">
                            <input type="checkbox" aria-label={model.display_name} disabled={!userId} checked={assignedIds.includes(model.model_id)} onChange={() => toggleAssignment(model.model_id)} className="accent-[#58ed87]" />
                            <span className="min-w-0 truncate">{model.display_name}</span>
                        </label>
                    ))}
                    {unavailableAssigned.map((id) => (
                        <label key={id} className="flex min-w-0 gap-2 rounded-lg border border-[#594d2a] bg-[#171408] p-3 text-sm text-[#d8c981]">
                            <input type="checkbox" aria-label={`取消不可用模型 ${id}`} checked onChange={() => toggleAssignment(id)} className="accent-[#d8c981]" />
                            <span className="min-w-0 truncate">{id} · 当前不可用，可取消</span>
                        </label>
                    ))}
                </div>
                <div className="mt-4 flex flex-wrap items-center gap-3">
                    <button type="button" disabled={!userId || status === "saving"} onClick={() => void saveAssignments()} className="rounded bg-[#42d977] px-4 py-2 text-sm font-semibold text-[#041008] disabled:opacity-40">
                        保存派发
                    </button>
                    {status === "saved" && (
                        <span role="status" className="text-sm text-[#58d881]">
                            派发已保存
                        </span>
                    )}
                </div>
            </section>
        </section>
    );
}
