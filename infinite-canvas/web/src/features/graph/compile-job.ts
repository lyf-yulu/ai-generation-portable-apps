import type { JobRequest, ModelSpec } from "@/api/contracts";
import { parameterControls } from "@/components/model-picker";
import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";
import { deriveResultAssetId } from "./contracts";
import { declaredModelPorts } from "./model-capabilities";

export type FrozenGraphJob = Readonly<{
    operation: JobRequest["operation"];
    model_id: string;
    prompt: string;
    params: Readonly<Record<string, unknown>>;
    asset_ids: readonly string[];
    inputs: Readonly<Record<string, readonly string[]>>;
}>;

export class CompileJobError extends Error {
    constructor(message: string) { super(message); this.name = "CompileJobError"; }
}

function validateParameter(control: ReturnType<typeof parameterControls>[number], value: unknown) {
    if (value === undefined) return !control.required;
    if (control.type === "enum") return control.enum?.some((candidate) => Object.is(candidate, value)) === true;
    if (control.type === "string") return typeof value === "string";
    if (control.type === "preset") return typeof value === "string";
    if (control.type === "boolean") return typeof value === "boolean";
    return typeof value === "number" && Number.isFinite(value) && (control.type !== "integer" || Number.isInteger(value))
        && (control.minimum === undefined || value >= control.minimum) && (control.maximum === undefined || value <= control.maximum);
}

export function compileGraphJob(nodes: readonly CanvasNodeData[], connections: readonly CanvasConnection[], modelNodeId: string, model: ModelSpec): FrozenGraphJob {
    const nodeMap = new Map(nodes.map((node) => [node.id, node]));
    const modelNode = nodeMap.get(modelNodeId);
    const graph = modelNode?.metadata?.graph;
    if (graph?.role !== "model" || graph.modelId !== model.model_id || !model.operations.includes(graph.operation as JobRequest["operation"])) throw new CompileJobError("模型节点与模型声明不一致。");
    const ports = declaredModelPorts(model);
    const portMap = new Map(ports.map((port) => [port.port_id, port]));
    const incoming = connections.filter((connection) => connection.toNodeId === modelNodeId && portMap.has(connection.toPortId));
    const promptEdges = incoming.filter((connection) => connection.toPortId === "prompt");
    if (promptEdges.length !== 1) throw new CompileJobError("请连接一个提示词节点。");
    const promptGraph = nodeMap.get(promptEdges[0].fromNodeId)?.metadata?.graph;
    if (promptGraph?.role !== "prompt" || !promptGraph.text.trim()) throw new CompileJobError("提示词不能为空。");

    const inputs: Record<string, readonly string[]> = {};
    for (const port of ports) {
        if (port.media_type === "text") continue;
        const assetIds: string[] = [];
        for (const connection of incoming.filter((edge) => edge.toPortId === port.port_id)) {
            const sourceNode = nodeMap.get(connection.fromNodeId);
            const source = sourceNode?.metadata?.graph;
            if (source?.role === "media-collection" && source.mediaType === port.media_type) {
                assetIds.push(...source.items.map((item) => item.assetId));
            } else if (source?.role === "result" && source.mediaType === port.media_type) {
                const assetId = source.assetId ?? deriveResultAssetId(sourceNode?.metadata);
                if (!assetId) throw new CompileJobError(`${port.port_id} 的连接类型不正确。`);
                assetIds.push(assetId);
            } else {
                throw new CompileJobError(`${port.port_id} 的连接类型不正确。`);
            }
        }
        if (assetIds.length < port.min_items) throw new CompileJobError(`${port.port_id} 至少需要 ${port.min_items} 个输入。`);
        if (assetIds.length > port.max_items) throw new CompileJobError(`${port.port_id} 最多允许 ${port.max_items} 个输入。`);
        if (assetIds.length) inputs[port.port_id] = Object.freeze(assetIds);
    }

    const controls = parameterControls(model.parameter_schema);
    const declared = new Map(controls.map((control) => [control.name, control]));
    const publicMappings = model.parameter_mappings && Object.keys(model.parameter_mappings).length ? model.parameter_mappings : null;
    if (Object.keys(graph.parameters).some((name) => !declared.has(name) || publicMappings && !(name in publicMappings))) throw new CompileJobError("模型节点包含不支持的参数。");
    if (controls.some((control) => !validateParameter(control, graph.parameters[control.name]))) throw new CompileJobError("请填写有效的模型参数。");
    const params = Object.freeze(Object.fromEntries(controls.filter((control) => graph.parameters[control.name] !== undefined).map((control) => [control.name, graph.parameters[control.name]])));
    const operation = graph.operation === "image.generate" && inputs.reference_images?.length && model.operations.includes("image.edit") ? "image.edit" : graph.operation as JobRequest["operation"];
    return Object.freeze({ operation, model_id: graph.modelId, prompt: promptGraph.text, params, asset_ids: Object.freeze([]), inputs: Object.freeze(inputs) });
}
