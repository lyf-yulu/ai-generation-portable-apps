import { Play, Sparkles } from "lucide-react";
import { useMemo } from "react";

import type { ModelSpec } from "@/api/contracts";
import { parameterControls } from "@/components/model-picker";
import type { GraphModelMetadata, GraphParameterValue } from "@/features/graph/contracts";
import { declaredModelPorts, graphPortsForModel } from "@/features/graph/model-capabilities";
import type { CanvasNodeData } from "@/types/canvas";

type Props = {
    node: CanvasNodeData;
    models: readonly ModelSpec[];
    disabled?: boolean;
    message?: string;
    onChange: (graph: GraphModelMetadata) => void;
    onRun: () => void;
    onRetry?: (token: string) => void;
    onCancel?: (jobId: string) => void;
};

function defaults(model: ModelSpec) {
    return Object.fromEntries(parameterControls(model.parameter_schema).flatMap((control) => (control.default === undefined ? [] : [[control.name, control.default]]))) as Record<string, GraphParameterValue>;
}

export function ModelCallNode({ node, models, disabled = false, message, onChange, onRun, onRetry, onCancel }: Props) {
    const graph = node.metadata?.graph;
    if (graph?.role !== "model") return null;
    const selected = models.find((model) => model.model_id === graph.modelId) ?? models[0];
    const controls = useMemo(() => parameterControls(selected?.parameter_schema ?? {}), [selected]);
    const visibleControls = controls.filter((control) => !control.visibleWhen || Object.is(graph.parameters[control.visibleWhen.name], control.visibleWhen.equals));
    const busy = node.metadata?.status === "loading" || node.metadata?.jobStatus === "queued" || node.metadata?.jobStatus === "running";
    const editDisabled = disabled || busy;
    if (!selected) return <article className="rounded-xl border border-[#6b4b2c] bg-[#171008] p-3 text-xs text-[#ffbd73]">暂无可用模型。</article>;
    const updateParameter = (name: string, value: GraphParameterValue) => onChange({ ...graph, parameters: { ...graph.parameters, [name]: value } });
    const choose = (modelId: string) => {
        const next = models.find((model) => model.model_id === modelId);
        if (!next) return;
        onChange({ ...graph, modelId, operation: next.operations[0], inputPorts: graphPortsForModel(next), parameters: defaults(next) });
    };
    return (
        <article className="max-w-full overflow-hidden rounded-xl border border-[#285038] bg-[#0a140e] text-xs text-[#dceee1] shadow-xl">
            <header className="flex items-center gap-2 border-b border-[#1c3826] px-3 py-2">
                <Sparkles className="size-4 text-[#58ed87]" />
                <strong>{node.title}</strong>
            </header>
            <div className="space-y-3 p-3" data-canvas-no-zoom>
                <p role="status" className="text-[11px] text-[#9fb5a5]">
                    任务状态：{node.metadata?.jobStatus === "queued" ? "排队中，可取消" : node.metadata?.jobStatus === "running" ? "运行中（平台不支持取消运行中任务）" : node.metadata?.status === "loading" ? "提交中" : node.metadata?.status === "success" ? "已完成" : node.metadata?.status === "error" ? "失败，可修改后重试" : "待运行"}
                </p>
                <label className="block text-[11px] text-[#9fb5a5]">
                    模型
                    <select aria-label="模型" disabled={editDisabled} value={selected.model_id} onChange={(event) => choose(event.target.value)} className="mt-1 block w-full rounded-md border border-[#285038] bg-[#050806] p-2 text-[#dceee1]">
                        {models.map((model) => (
                            <option key={model.model_id} value={model.model_id}>
                                {model.display_name}
                            </option>
                        ))}
                    </select>
                </label>
                <div className="flex flex-wrap gap-1 text-[10px] text-[#8fa596]">
                    {declaredModelPorts(selected).map((port) => (
                        <span key={port.port_id} className="rounded border border-[#264532] px-1.5 py-1">
                            {port.port_id === "prompt" ? "提示词" : port.port_id}：{port.max_items}
                        </span>
                    ))}
                </div>
                {visibleControls.map((control) => (
                    <label key={control.name} className="block text-[11px] text-[#9fb5a5]">
                        {control.title ?? control.name}
                        {control.type === "enum" ? (
                            <select
                                aria-label={control.title ?? control.name}
                                disabled={editDisabled}
                                value={String(control.enum?.findIndex((value) => Object.is(value, graph.parameters[control.name])) ?? 0)}
                                onChange={(event) => updateParameter(control.name, control.enum?.[Number(event.target.value)] ?? null)}
                                className="mt-1 block w-full rounded-md border border-[#285038] bg-[#050806] p-2"
                            >
                                {control.enum?.map((value, index) => (
                                    <option key={String(value)} value={index}>
                                        {String(value)}
                                    </option>
                                ))}
                            </select>
                        ) : control.type === "boolean" ? (
                            <input
                                aria-label={control.title ?? control.name}
                                disabled={editDisabled}
                                type="checkbox"
                                checked={graph.parameters[control.name] === true}
                                onChange={(event) => updateParameter(control.name, event.target.checked)}
                                className="ml-2 accent-[#58ed87]"
                            />
                        ) : (
                            <input
                                aria-label={control.title ?? control.name}
                                disabled={editDisabled}
                                type={control.type === "number" || control.type === "integer" ? "number" : "text"}
                                min={control.minimum}
                                max={control.maximum}
                                step={control.type === "integer" ? 1 : undefined}
                                value={String(graph.parameters[control.name] ?? "")}
                                onChange={(event) => updateParameter(control.name, control.type === "number" || control.type === "integer" ? event.target.value === "" ? null : Number(event.target.value) : event.target.value)}
                                className="mt-1 block w-full rounded-md border border-[#285038] bg-[#050806] p-2"
                            />
                        )}
                        {control.description ? <span className="mt-1 block text-[10px] leading-4 text-[#789080]">{control.description}</span> : null}
                    </label>
                ))}
                <button
                    type="button"
                    disabled={editDisabled}
                    onClick={node.metadata?.status === "error" && node.metadata.idempotencyKey && onRetry ? () => onRetry(node.metadata!.idempotencyKey!) : onRun}
                    className="flex w-full items-center justify-center gap-2 rounded-lg bg-[#47d978] px-3 py-2 font-semibold text-[#041008] disabled:opacity-40"
                >
                    <Play className="size-3.5" />
                    {node.metadata?.status === "error" && node.metadata.idempotencyKey ? "使用原任务键重试" : "运行模型"}
                </button>
                {node.metadata?.jobStatus === "queued" && node.metadata.jobId && onCancel ? (
                    <button type="button" disabled={disabled} onClick={() => onCancel(node.metadata!.jobId!)} className="w-full rounded-lg border border-[#6b4b2c] px-3 py-2 text-[#ffbd73] disabled:opacity-40">
                        取消排队任务
                    </button>
                ) : null}
                {message ? (
                    <p role="status" className="text-[#ffbd73]">
                        {message}
                    </p>
                ) : null}
            </div>
        </article>
    );
}
