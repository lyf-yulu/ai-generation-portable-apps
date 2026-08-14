import { useEffect, useMemo, useRef, useState } from "react";

import type { AdminCredentialPool, AdminLogicalModel, AdminModelRoute, ModelRouteWrite } from "@/api/admin";
import { ApiRequestError } from "@/api/client";
import { routeContractForModel, routeTemplatesForModel, templateForModel, templateForRoute, type TemplateId } from "./model-templates";

type Props = {
    model: AdminLogicalModel;
    route: AdminModelRoute | null;
    pools: AdminCredentialPool[];
    onSave: (body: ModelRouteWrite & { revision?: number }) => Promise<AdminModelRoute>;
    onSaved: (updated: AdminModelRoute) => void;
    onRefresh?: () => void;
};

export function ModelRouteEditor({ model, route, pools, onSave, onSaved, onRefresh }: Props) {
    const templates = routeTemplatesForModel(model);
    const baseTemplate = templateForRoute(route) || routeTemplatesForModel(model)[0] || templateForModel(model);
    const initialProvider = route?.provider_id || "";
    const [form, setForm] = useState({
        route_id: route?.route_id || "", provider_id: initialProvider, provider_model_name: route?.provider_model_name || "",
        template_id: baseTemplate.id as TemplateId,
        pool_id: route?.credential_pool_ref || "", priority: route?.priority ?? 100, max_concurrency: route?.max_concurrency ?? 1,
    });
    const [saving, setSaving] = useState(false);
    const [message, setMessage] = useState<"" | "failed" | "conflict">("");
    const version = useRef(0);
    const savingRef = useRef(false);
    useEffect(() => {
        version.current += 1;
        const template = templateForRoute(route) || routeTemplatesForModel(model)[0] || templateForModel(model);
        setForm({ route_id: route?.route_id || "", provider_id: route?.provider_id || "", provider_model_name: route?.provider_model_name || "", template_id: template.id, pool_id: route?.credential_pool_ref || "", priority: route?.priority ?? 100, max_concurrency: route?.max_concurrency ?? 1 });
        savingRef.current = false; setSaving(false); setMessage("");
        return () => { version.current += 1; savingRef.current = false; };
    }, [model.model_id, model.revision, route?.route_id, route?.revision]);
    const template = templates.find((item) => item.id === form.template_id) || templates[0] || baseTemplate;
    const routeContract = routeContractForModel(template, model);
    const unsupportedExisting = Boolean(route && (route.adapter_type !== template.adapter_type || route.family !== template.familyHint || route.operation_contracts?.[0]?.operation !== template.contract.operation));
    const effectiveFamily = route?.family || template.familyHint;
    const compatiblePools = useMemo(() => pools.filter((pool) => pool.adapter_type === template.adapter_type && pool.allowed_families.includes(effectiveFamily)), [effectiveFamily, pools, template.adapter_type]);
    const providers = useMemo(() => [...new Set(compatiblePools.map((pool) => pool.provider_id))].sort(), [compatiblePools]);
    const availablePools = useMemo(() => compatiblePools.filter((pool) => pool.provider_id === form.provider_id), [compatiblePools, form.provider_id]);
    useEffect(() => {
        if (route && form.route_id !== route.route_id) return;
        if (form.pool_id && !availablePools.some((pool) => pool.pool_id === form.pool_id)) setForm((current) => ({ ...current, pool_id: "" }));
    }, [availablePools, form.pool_id, form.route_id, route]);
    const missingFields = [
        !form.route_id.trim() && "线路 ID",
        !form.provider_id && "Provider",
        !form.provider_model_name.trim() && "供应商模型名",
        !form.pool_id && "凭据池",
    ].filter(Boolean) as string[];
    const canSubmit = !saving && !unsupportedExisting && compatiblePools.length > 0 && missingFields.length === 0;
    const submit = async () => {
        if (savingRef.current || !form.route_id.trim() || !form.provider_id || !form.provider_model_name.trim() || !form.pool_id) return;
        const requestVersion = version.current;
        savingRef.current = true;
        setSaving(true); setMessage("");
        try {
            const updated = await onSave({ route_id: form.route_id.trim(), model_id: model.model_id, provider_id: form.provider_id, provider_model_name: form.provider_model_name.trim(), adapter_type: route?.adapter_type || template.adapter_type, credential_pool_ref: form.pool_id, family: route?.family || template.familyHint, operation_contracts: route?.operation_contracts || [routeContract], priority: form.priority, max_concurrency: form.max_concurrency, enabled: route?.enabled ?? false, ...(route ? { revision: route.revision } : {}) });
            if (version.current === requestVersion) onSaved(updated);
        } catch (error) {
            if (version.current === requestVersion) setMessage(error instanceof ApiRequestError && error.code === "REVISION_CONFLICT" ? "conflict" : "failed");
        } finally { if (version.current === requestVersion) { savingRef.current = false; setSaving(false); } }
    };
    return <section className="min-w-0 rounded-xl border border-[#245a35] bg-[#07110b] p-4 text-[#e5f5e9]">
        <h2 className="text-lg font-semibold">{route ? "编辑调用线路" : "新建调用线路"}</h2>
        <p className="mt-1 text-xs text-[#86a991]">凭据由部署配置管理；这里仅绑定兼容的安全凭据池。</p>
        <form className="mt-4 grid min-w-0 gap-3 sm:grid-cols-2" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
            <label className="min-w-0 text-sm">线路 ID<input aria-label="线路 ID" disabled={Boolean(route)} value={form.route_id} onChange={(event) => setForm({ ...form, route_id: event.target.value })} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2 disabled:text-[#829889]" /></label>
            <label className="min-w-0 text-sm">线路模板<select aria-label="线路模板" disabled={Boolean(route)} value={form.template_id} onChange={(event) => { const next = templates.find((item) => item.id === event.target.value) || template; setForm({ ...form, template_id: next.id, pool_id: "" }); }} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2 disabled:text-[#829889]">{templates.map((item) => <option key={item.id} value={item.id}>{item.routeLabel}</option>)}</select></label>
            <label className="min-w-0 text-sm">Provider<select aria-label="Provider" value={form.provider_id} onChange={(event) => setForm({ ...form, provider_id: event.target.value, pool_id: "" })} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2"><option value="">请选择</option>{providers.map((provider) => <option key={provider} value={provider}>{provider}</option>)}</select></label>
            <div className="min-w-0 text-sm">模型族<div aria-label="模型族" className="mt-1 min-h-10 w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2 font-mono text-[#9ad7ab]">{effectiveFamily}</div></div>
            <label className="min-w-0 text-sm">供应商模型名<input aria-label="供应商模型名" value={form.provider_model_name} onChange={(event) => setForm({ ...form, provider_model_name: event.target.value })} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2" /></label>
            <label className="min-w-0 text-sm">凭据池<select aria-label="凭据池" value={form.pool_id} onChange={(event) => setForm({ ...form, pool_id: event.target.value })} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2"><option value="">请选择兼容池</option>{availablePools.map((pool) => <option key={pool.pool_id} value={pool.pool_id}>{pool.pool_id} · {pool.group}</option>)}</select></label>
            <label className="min-w-0 text-sm">优先级<input aria-label="优先级" type="number" min={0} max={1_000_000} value={form.priority} onChange={(event) => setForm({ ...form, priority: Number(event.target.value) })} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2" /></label>
            <label className="min-w-0 text-sm">最大并发<input aria-label="最大并发" type="number" min={1} max={4096} value={form.max_concurrency} onChange={(event) => setForm({ ...form, max_concurrency: Number(event.target.value) })} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2" /></label>
            <div className="sm:col-span-2"><button type="submit" disabled={!canSubmit} className="rounded bg-[#42d977] px-4 py-2 text-sm font-semibold text-[#041008] disabled:opacity-50">{saving ? "保存中…" : "保存线路"}</button></div>
        </form>
        {!route && compatiblePools.length === 0 && <p role="alert" className="mt-3 text-sm text-[#ffbd73]">尚未配置兼容的{model.modality === "video" ? "视频" : "图像"}凭据池；请先由部署管理员添加对应 Provider、模型族和凭据池。</p>}
        {!route && compatiblePools.length > 0 && missingFields.length > 0 && <p role="status" className="mt-3 text-sm text-[#86a991]">保存前请填写：{missingFields.join("、")}。</p>}
        {unsupportedExisting && <p role="alert" className="mt-3 text-sm text-[#ffbd73]">该历史线路不属于当前受信模板，已设为只读；请停用后新建兼容线路。</p>}
        {message === "failed" && <p role="alert" className="mt-3 text-sm text-[#ffbd73]">线路未保存，请检查 Provider、模型族和凭据池是否兼容。</p>}
        {message === "conflict" && <div role="alert" className="mt-3 flex gap-2 text-sm text-[#ffbd73]">配置已变化，请重新加载。{onRefresh && <button type="button" onClick={onRefresh} className="underline">重新加载</button>}</div>}
        <div className="mt-5 grid min-w-0 gap-2 sm:grid-cols-2" aria-label="安全凭据池状态">{pools.map((pool) => <article key={pool.pool_id} className="min-w-0 rounded-lg border border-[#1e482b] bg-[#0a1710] p-3 text-xs">
            <h3 className="truncate font-medium text-[#8ff0aa]">{pool.pool_id}</h3>
            <p className="mt-1 text-[#a9c6b0]">{pool.provider_id} · {pool.group} · {pool.key_count} 把凭据</p>
            <p className="mt-1 text-[#86a991]">{pool.capacity_status === "available" ? `可用 ${pool.available_count ?? 0} · 忙碌 ${pool.busy_count ?? 0}` : "容量状态暂不可用"}</p>
        </article>)}</div>
    </section>;
}
