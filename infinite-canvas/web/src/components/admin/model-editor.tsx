import { useEffect, useRef, useState } from "react";

import type { AdminLogicalModel, LogicalModelWrite } from "@/api/admin";
import { ApiRequestError } from "@/api/client";
import { ADMIN_MODEL_TEMPLATES, CAPABILITY_TEMPLATES, templateForModel, type CapabilityId, type ModelProfileId } from "./model-templates";

type Props = {
    model: AdminLogicalModel | null;
    onSave: (body: LogicalModelWrite & { revision?: number }) => Promise<AdminLogicalModel>;
    onSaved: (updated: AdminLogicalModel) => void;
    onRefresh?: () => void;
};

export function ModelEditor({ model, onSave, onSaved, onRefresh }: Props) {
    const initial = templateForModel(model);
    const [form, setForm] = useState({ model_id: model?.model_id || "", display_name: model?.display_name || "", introduction: model?.introduction || "", capability_id: initial.capability, template_id: initial.id });
    const [saving, setSaving] = useState(false);
    const [message, setMessage] = useState<"" | "failed" | "conflict">("");
    const version = useRef(0);
    const savingRef = useRef(false);
    useEffect(() => {
        version.current += 1;
        const template = templateForModel(model);
        setForm({ model_id: model?.model_id || "", display_name: model?.display_name || "", introduction: model?.introduction || "", capability_id: template.capability, template_id: template.id });
        savingRef.current = false; setSaving(false); setMessage("");
        return () => { version.current += 1; savingRef.current = false; };
    }, [model?.model_id, model?.revision]);
    const template = ADMIN_MODEL_TEMPLATES.find((item) => item.id === form.template_id) || ADMIN_MODEL_TEMPLATES[0];
    const profiles = ADMIN_MODEL_TEMPLATES.filter((item) => item.capability === form.capability_id);
    const submit = async () => {
        if (savingRef.current || !form.model_id.trim() || !form.display_name.trim() || !form.introduction.trim()) return;
        const requestVersion = version.current;
        savingRef.current = true;
        setSaving(true); setMessage("");
        try {
            const updated = await onSave({ model_id: form.model_id.trim(), display_name: form.display_name.trim(), introduction: form.introduction.trim(), modality: model?.modality ?? template.modality, operation_contracts: model?.operation_contracts || [template.contract], enabled: model?.enabled ?? false, ...(model ? { revision: model.revision } : {}) });
            if (version.current === requestVersion) onSaved(updated);
        } catch (error) {
            if (version.current === requestVersion) setMessage(error instanceof ApiRequestError && error.code === "REVISION_CONFLICT" ? "conflict" : "failed");
        } finally { if (version.current === requestVersion) { savingRef.current = false; setSaving(false); } }
    };
    return <form className="min-w-0 rounded-xl border border-[#245a35] bg-[#07110b] p-4 text-[#e5f5e9]" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <h2 className="text-lg font-semibold">{model ? "编辑逻辑模型" : "新建逻辑模型"}</h2>
        <div className="mt-4 grid min-w-0 gap-3 sm:grid-cols-2">
            <label className="min-w-0 text-sm">模型 ID<input aria-label="模型 ID" disabled={Boolean(model)} value={form.model_id} onChange={(event) => setForm({ ...form, model_id: event.target.value })} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2 disabled:text-[#829889]" /></label>
            <label className="min-w-0 text-sm">模型显示名<input aria-label="模型显示名" value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2" /></label>
            <label className="min-w-0 text-sm sm:col-span-2">模型介绍<textarea aria-label="模型介绍" value={form.introduction} onChange={(event) => setForm({ ...form, introduction: event.target.value })} className="mt-1 block min-h-20 w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2" /></label>
            <label className="min-w-0 text-sm">能力模板<select aria-label="能力模板" disabled={Boolean(model)} value={form.capability_id} onChange={(event) => { const capability = event.target.value as CapabilityId; const first = ADMIN_MODEL_TEMPLATES.find((item) => item.capability === capability); if (first) setForm({ ...form, capability_id: capability, template_id: first.id }); }} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2">{CAPABILITY_TEMPLATES.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
            <label className="min-w-0 text-sm">模型类型<select aria-label="模型类型" disabled={Boolean(model)} value={form.template_id} onChange={(event) => setForm({ ...form, template_id: event.target.value as ModelProfileId })} className="mt-1 block w-full min-w-0 rounded border border-[#285038] bg-[#0b1710] px-3 py-2">{profiles.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
        </div>
        <p className="mt-3 text-xs text-[#86a991]">操作：<span className="font-mono text-[#70e795]">{template.contract.operation}</span>。模型创建后 ID 与能力类别保持稳定。</p>
        <button type="submit" disabled={saving} className="mt-4 rounded bg-[#42d977] px-4 py-2 text-sm font-semibold text-[#041008] disabled:opacity-50">{saving ? "保存中…" : "保存模型"}</button>
        {message === "failed" && <p role="alert" className="mt-3 text-sm text-[#ffbd73]">模型未保存，请检查填写内容。</p>}
        {message === "conflict" && <div role="alert" className="mt-3 flex flex-wrap items-center gap-2 text-sm text-[#ffbd73]">配置已变化，请重新加载。{onRefresh && <button type="button" onClick={onRefresh} className="underline">重新加载</button>}</div>}
    </form>;
}
