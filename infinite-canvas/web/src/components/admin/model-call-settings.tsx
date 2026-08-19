import { useEffect, useMemo, useRef, useState } from "react";

import type { AdminCredentialPool, AdminLogicalModel, AdminModelRoute, ModelRouteWrite } from "@/api/admin";
import { ApiRequestError } from "@/api/client";
import { callingPresetsForModel, routeMatchesCallingPreset, type AdminCallingPreset } from "./model-templates";

type LifecycleAction = "enable" | "disable";

type Props = {
    model: AdminLogicalModel;
    routes: AdminModelRoute[];
    pools: AdminCredentialPool[];
    onCreate: (body: ModelRouteWrite) => Promise<AdminModelRoute>;
    onUpdate: (body: ModelRouteWrite & { revision: number }) => Promise<AdminModelRoute>;
    onLifecycle: (route: AdminModelRoute, action: LifecycleAction) => Promise<AdminModelRoute>;
    onSaved: (updated: AdminModelRoute) => void;
    onRefresh?: () => void;
};

function CallingPresetCard({ model, preset, routes, pools, onCreate, onUpdate, onLifecycle, onSaved, onRefresh }: Props & { preset: AdminCallingPreset }) {
    const matchingRoutes = useMemo(() => routes.filter((route) => !route.archived_at && routeMatchesCallingPreset(route, preset)), [preset, routes]);
    const route = matchingRoutes.length === 1 ? matchingRoutes[0] : null;
    const compatiblePools = useMemo(() => pools.filter((pool) => pool.provider_id === preset.providerId && pool.adapter_type === preset.adapterType && pool.allowed_families.includes(preset.family)), [pools, preset]);
    const [form, setForm] = useState({ poolId: route?.credential_pool_ref || "", priority: route?.priority ?? 100, maxConcurrency: route?.max_concurrency ?? 1, enabled: route?.enabled ?? false });
    const [saving, setSaving] = useState(false);
    const [message, setMessage] = useState<"" | "failed" | "conflict">("");
    const version = useRef(0);
    const savingRef = useRef(false);

    useEffect(() => {
        version.current += 1;
        setForm({ poolId: route?.credential_pool_ref || "", priority: route?.priority ?? 100, maxConcurrency: route?.max_concurrency ?? 1, enabled: route?.enabled ?? false });
        savingRef.current = false;
        setSaving(false);
        setMessage("");
        return () => {
            version.current += 1;
            savingRef.current = false;
        };
    }, [model.model_id, model.revision, preset.id, route?.route_id, route?.revision]);

    const hasCompatibleSelection = compatiblePools.some((pool) => pool.pool_id === form.poolId);
    const duplicate = matchingRoutes.length > 1;
    const canSave = !saving && !duplicate && hasCompatibleSelection;
    const write = (): ModelRouteWrite => ({
        route_id: route?.route_id || `${model.model_id}-${preset.id}`,
        model_id: model.model_id,
        provider_id: preset.providerId,
        provider_model_name: preset.providerModelName,
        adapter_type: preset.adapterType,
        credential_pool_ref: form.poolId,
        family: preset.family,
        operation_contracts: [preset.contract],
        priority: form.priority,
        max_concurrency: form.maxConcurrency,
        enabled: route?.enabled ?? form.enabled,
    });
    const request = async (operation: () => Promise<AdminModelRoute>) => {
        if (savingRef.current) return;
        const requestVersion = version.current;
        savingRef.current = true;
        setSaving(true);
        setMessage("");
        try {
            const updated = await operation();
            if (version.current === requestVersion) onSaved(updated);
        } catch (error) {
            if (version.current === requestVersion) setMessage(error instanceof ApiRequestError && error.code === "REVISION_CONFLICT" ? "conflict" : "failed");
        } finally {
            if (version.current === requestVersion) {
                savingRef.current = false;
                setSaving(false);
            }
        }
    };
    const save = () => {
        if (!canSave) return;
        void request(() => (route ? onUpdate({ ...write(), revision: route.revision }) : onCreate(write())));
    };
    const changeEnabled = (enabled: boolean) => {
        if (route) void request(() => onLifecycle(route, enabled ? "enable" : "disable"));
        else setForm((current) => ({ ...current, enabled }));
    };

    return (
        <article className="min-w-0 rounded-xl border border-[#1e482b] bg-[#0a1710] p-4" aria-label={`${preset.label} 调用设置`}>
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h3 className="text-base font-semibold text-[#8ff0aa]">{preset.label}</h3>
                    <p className="mt-1 text-xs text-[#86a991]">调用协议由内置预置维护；只可调整池、优先级、并发与启用状态。</p>
                </div>
                <label className="flex items-center gap-2 text-sm text-[#c9decf]">
                    <input
                        aria-label={`启用 ${preset.label}`}
                        type="checkbox"
                        checked={route?.enabled ?? form.enabled}
                        disabled={saving || (!(route?.enabled) && !hasCompatibleSelection)}
                        onChange={(event) => changeEnabled(event.target.checked)}
                        className="accent-[#58ed87]"
                    />
                    启用
                </label>
            </div>
            {duplicate ? (
                <p role="alert" className="mt-3 text-sm text-[#ffbd73]">
                    发现 {matchingRoutes.length} 条匹配的 {preset.label} 线路，无法安全选择其中一条；请在历史审计中确认重复记录后处理。
                </p>
            ) : (
                <>
                    <div className="mt-4 grid min-w-0 gap-3 sm:grid-cols-3">
                        <label className="min-w-0 text-sm">
                            {preset.label} 凭据池
                            <select
                                aria-label={`${preset.label} 凭据池`}
                                value={form.poolId}
                                onChange={(event) => setForm((current) => ({ ...current, poolId: event.target.value }))}
                                className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2"
                            >
                                <option value="">请选择兼容池</option>
                                {compatiblePools.map((pool) => (
                                    <option key={pool.pool_id} value={pool.pool_id}>
                                        {pool.pool_id} · {pool.group}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <label className="min-w-0 text-sm">
                            {preset.label} 优先级
                            <input
                                aria-label={`${preset.label} 优先级`}
                                type="number"
                                min={0}
                                max={1_000_000}
                                value={form.priority}
                                onChange={(event) => setForm((current) => ({ ...current, priority: Number(event.target.value) }))}
                                className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2"
                            />
                        </label>
                        <label className="min-w-0 text-sm">
                            {preset.label} 最大并发
                            <input
                                aria-label={`${preset.label} 最大并发`}
                                type="number"
                                min={1}
                                max={4096}
                                value={form.maxConcurrency}
                                onChange={(event) => setForm((current) => ({ ...current, maxConcurrency: Number(event.target.value) }))}
                                className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2"
                            />
                        </label>
                    </div>
                    <button type="button" disabled={!canSave} onClick={save} className="mt-4 rounded bg-[#42d977] px-4 py-2 text-sm font-semibold text-[#041008] disabled:opacity-50">
                        {saving ? "保存中…" : `保存 ${preset.label} 设置`}
                    </button>
                    {compatiblePools.length === 0 && (
                        <p role="alert" className="mt-3 text-sm text-[#ffbd73]">
                            尚未配置与此调用方式精确匹配的凭据池，因此不能启用。
                        </p>
                    )}
                    {compatiblePools.length > 0 && !hasCompatibleSelection && (
                        <p role="status" className="mt-3 text-sm text-[#86a991]">
                            请选择兼容凭据池后保存。
                        </p>
                    )}
                </>
            )}
            {message === "failed" && (
                <p role="alert" className="mt-3 text-sm text-[#ffbd73]">
                    调用设置未保存，请检查凭据池状态。
                </p>
            )}
            {message === "conflict" && (
                <div role="alert" className="mt-3 flex flex-wrap gap-2 text-sm text-[#ffbd73]">
                    配置已变化，请重新加载。
                    {onRefresh && (
                        <button type="button" onClick={onRefresh} className="underline">
                            重新加载
                        </button>
                    )}
                </div>
            )}
            <div className="mt-4 grid min-w-0 gap-2 sm:grid-cols-2" aria-label={`${preset.label} 安全池状态`}>
                {compatiblePools.map((pool) => (
                    <div key={pool.pool_id} className="min-w-0 rounded-lg border border-[#1e482b] bg-[#07110b] p-3 text-xs">
                        <p className="truncate text-[#a9c6b0]">
                            {pool.pool_id} · {pool.group}
                        </p>
                        <p className="mt-1 text-[#86a991]">{pool.capacity_status === "available" ? `可用 ${pool.available_count ?? 0} · 忙碌 ${pool.busy_count ?? 0}` : "容量状态暂不可用"}</p>
                    </div>
                ))}
            </div>
        </article>
    );
}

export function ModelCallSettings(props: Props) {
    const presets = callingPresetsForModel(props.model);
    return (
        <section className="min-w-0 rounded-xl border border-[#245a35] bg-[#07110b] p-4 text-[#e5f5e9]">
            <div>
                <h2 className="text-lg font-semibold">调用设置</h2>
                <p className="mt-1 text-xs text-[#86a991]">选择受信调用方式的兼容凭据池。真实凭据始终由部署配置管理。</p>
            </div>
            <div className="mt-4 grid min-w-0 gap-3">
                {presets.map((preset) => (
                    <CallingPresetCard key={preset.id} {...props} preset={preset} />
                ))}
            </div>
        </section>
    );
}
