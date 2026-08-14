import { useEffect, useMemo } from "react";

import type { ModelOperation, ModelSpec } from "@/api/contracts";
import { modelsForOperation, parameterControls, type ParameterControl } from "@/components/model-picker";


export type GenerationInspectorValue = { prompt: string; modelId: string; params: Record<string, unknown> };
type Props = {
    models: ModelSpec[];
    operation: ModelOperation;
    value: GenerationInspectorValue;
    disabled?: boolean;
    message?: string;
    onChange: (value: GenerationInspectorValue) => void;
    onSubmit: (model: ModelSpec, safeParams: Record<string, unknown>) => void;
};

function defaultsFor(controls: ParameterControl[]) { return Object.fromEntries(controls.filter((control) => control.default !== undefined).map((control) => [control.name, control.default])); }
function invalid(control: ParameterControl, value: unknown) {
    if (value === undefined) return Boolean(control.required);
    if (control.type === "string") return typeof value !== "string";
    if (control.type === "boolean") return typeof value !== "boolean";
    if (control.type === "number" || control.type === "integer") return typeof value !== "number" || !Number.isFinite(value) || (control.type === "integer" && !Number.isInteger(value)) || (control.minimum !== undefined && value < control.minimum) || (control.maximum !== undefined && value > control.maximum);
    return !control.enum?.some((item) => Object.is(item, value));
}

export function GenerationInspector({ models, operation, value, disabled, message, onChange, onSubmit }: Props) {
    const available = useMemo(() => modelsForOperation(models, operation, "text"), [models, operation]);
    const selected = available.find((model) => model.model_id === value.modelId) || available[0];
    const isVideo = operation.startsWith("video.");
    const generationLabel = isVideo ? "视频生成" : "图片生成";
    const generationKicker = isVideo ? "VIDEO GENERATION" : "IMAGE GENERATION";
    const controls = useMemo(() => parameterControls(selected?.parameter_schema || {}), [selected]);
    const invalidParams = controls.some((control) => invalid(control, value.params[control.name]));
    const safeParams = Object.fromEntries(controls.filter((control) => value.params[control.name] !== undefined).map((control) => [control.name, value.params[control.name]]));

    useEffect(() => {
        if (selected && selected.model_id !== value.modelId) onChange({ ...value, modelId: selected.model_id, params: defaultsFor(controls) });
    }, [selected?.model_id]);

    const chooseModel = (modelId: string) => {
        const model = available.find((item) => item.model_id === modelId);
        onChange({ ...value, modelId, params: defaultsFor(parameterControls(model?.parameter_schema || {})) });
    };

    return <aside data-testid="generation-inspector" data-canvas-no-zoom className="max-h-[45%] shrink-0 overflow-auto border-t border-[#1d3d28] bg-[#08100b] p-4 text-[#dceee1] lg:h-full lg:max-h-none lg:border-l lg:border-t-0 lg:p-5">
        <p className="text-xs tracking-[0.18em] text-[#58ed87]">{generationKicker}</p><h2 className="mt-2 text-lg font-semibold">{generationLabel}</h2>
        <label className="mt-5 block text-sm" htmlFor="studio-prompt">提示词</label><textarea disabled={disabled} id="studio-prompt" className="mt-2 min-h-28 w-full resize-y rounded-lg border border-[#285038] bg-[#050806] p-3 text-sm disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#58ed87]" value={value.prompt} onChange={(event) => onChange({ ...value, prompt: event.target.value })} />
        <label className="mt-4 block text-sm">模型<select disabled={disabled} aria-label="模型" className="mt-2 block w-full rounded-lg border border-[#285038] bg-[#050806] p-2.5 disabled:cursor-not-allowed disabled:opacity-50" value={selected?.model_id || ""} onChange={(event) => chooseModel(event.target.value)}>{available.map((model) => <option key={model.model_id} value={model.model_id}>{model.display_name}</option>)}</select></label>
        {controls.map((control) => <label key={control.name} className="mt-4 block text-sm">{control.name}{control.type === "enum" ? <select disabled={disabled} aria-label={control.name} className="mt-2 block w-full rounded-lg border border-[#285038] bg-[#050806] p-2.5 disabled:cursor-not-allowed disabled:opacity-50" value={String((control.enum || []).findIndex((item) => Object.is(item, value.params[control.name])))} onChange={(event) => onChange({ ...value, params: { ...value.params, [control.name]: control.enum?.[Number(event.target.value)] } })}>{control.enum?.map((item, index) => <option key={index} value={index}>{String(item)}</option>)}</select> : control.type === "boolean" ? <input disabled={disabled} aria-label={control.name} type="checkbox" className="ml-3 accent-[#58ed87] disabled:cursor-not-allowed disabled:opacity-50" checked={value.params[control.name] === true} onChange={(event) => onChange({ ...value, params: { ...value.params, [control.name]: event.target.checked } })} /> : <input disabled={disabled} aria-label={control.name} className="mt-2 block w-full rounded-lg border border-[#285038] bg-[#050806] p-2.5 disabled:cursor-not-allowed disabled:opacity-50" value={value.params[control.name] === undefined ? "" : String(value.params[control.name])} onChange={(event) => onChange({ ...value, params: { ...value.params, [control.name]: control.type === "number" || control.type === "integer" ? (event.target.value === "" ? undefined : Number(event.target.value)) : event.target.value } })} />}</label>)}
        {invalidParams ? <p className="mt-3 text-sm text-[#ffbd73]">请填写有效参数。</p> : null}
        <button type="button" className="mt-5 w-full rounded-lg bg-[#47d978] px-4 py-2.5 text-sm font-semibold text-[#041008] disabled:opacity-40" disabled={disabled || !value.prompt.trim() || !selected || invalidParams} onClick={() => selected && onSubmit(selected, safeParams)}>加入任务队列</button>
        {message ? <p className="mt-3 text-sm text-[#ffbd73]">{message}</p> : null}<p className="mt-4 text-xs leading-5 text-[#688371]">仅通过当前站点的受控任务接口提交；服务密钥不会进入浏览器。</p>
    </aside>;
}
