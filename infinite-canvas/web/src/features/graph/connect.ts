import { STANDARD_MODEL_INPUT_PORTS, graphInputPortDescriptor, type GraphPortValueType } from "@/features/graph/contracts";
import { nodeRegistry, type NodeRegistry } from "@/features/nodes/registry";
import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

export type GraphPortRef = Readonly<{
    nodeId: string;
    portId: string;
    direction: "source" | "target";
    valueType?: GraphPortValueType;
    label?: string;
}>;

export type ResolvedConnectionState = Readonly<{
    connection: CanvasConnection;
    connectionKey: string;
    active: boolean;
    reason?: "duplicate" | "duplicate-id" | "opaque" | "missing-node" | "missing-port" | "incompatible" | "prompt-conflict";
}>;

export function graphConnectionInactiveMessage(reason: NonNullable<ResolvedConnectionState["reason"]>) {
    if (reason === "prompt-conflict") return "提示词冲突";
    if (reason === "incompatible") return "端口类型不兼容";
    if (reason === "missing-node") return "节点不存在";
    if (reason === "missing-port") return "端口不存在或已撤销";
    if (reason === "duplicate-id") return "连接 ID 重复";
    if (reason === "duplicate") return "连接端口重复";
    return "插件或端口暂不可用";
}

export function graphConnectionTransientKey(connection: CanvasConnection, index: number) {
    return `${index}:${JSON.stringify([connection.id, connection.fromNodeId, connection.fromPortId, connection.toNodeId, connection.toPortId])}`;
}

export type GraphConnectionResult =
    | { ok: true; connection: CanvasConnection }
    | { ok: false; reason: "self" | "duplicate" | "incompatible" | "prompt-occupied" };

export function graphConnectionRejectionMessage(reason: Extract<GraphConnectionResult, { ok: false }>["reason"]) {
    if (reason === "self") return "不能连接同一个节点。";
    if (reason === "duplicate") return "这两个端口已经连接。";
    if (reason === "prompt-occupied") return "该模型已有提示词连接，每个模型只允许一个提示词节点。";
    return "这两个端口类型不兼容。";
}

export const GRAPH_PORT_IDS = {
    prompt: "prompt",
    referenceImages: "reference_images",
    firstFrame: "first_frame",
    lastFrame: "last_frame",
    referenceVideo: "reference_video",
    referenceAudio: "reference_audio",
    result: "result",
    media: "media",
} as const;

export function getNodePorts(node: CanvasNodeData, registry: Pick<NodeRegistry, "getNode"> = nodeRegistry): { sources: GraphPortRef[]; targets: GraphPortRef[] } {
    const graph = node.metadata?.graph;
    if (graph?.role === "prompt") {
        return { sources: [sourcePort(node.id, graph.outputPortId, "prompt")], targets: [] };
    }
    if (graph?.role === "media-collection") {
        return { sources: [sourcePort(node.id, graph.outputPortId, graph.mediaType)], targets: [] };
    }
    if (graph?.role === "result") return {
        sources: [sourcePort(node.id, graph.outputPortId, graph.mediaType)],
        targets: [targetPort(node.id, graph.inputPortId, "result")],
    };
    if (graph?.role === "model") {
        const inputPorts = Array.isArray(graph.inputPorts)
            ? graph.inputPorts
            : ((graph as unknown as { inputPortIds?: unknown }).inputPortIds as unknown[] | undefined)?.filter((portId): portId is string => typeof portId === "string").map(graphInputPortDescriptor) ?? [];
        return {
            sources: [sourcePort(node.id, graph.outputPortId, "result")],
            targets: inputPorts.map((descriptor) => targetPort(node.id, descriptor.id, descriptor.accepts, descriptor.label)),
        };
    }
    if (graph?.role === "comfy-workflow") return {
        sources: [sourcePort(node.id, graph.outputPortId, "result")],
        targets: graph.inputPorts.map((descriptor) => targetPort(node.id, descriptor.id, descriptor.accepts, descriptor.label)),
    };
    const definition = registry.getNode(String(node.type));
    if (!definition) return { sources: [], targets: [] };
    return {
        sources: definition.outputs.map((declaration) => typeof declaration === "string"
            ? sourcePort(node.id, declaration, "any")
            : sourcePort(node.id, declaration.id, declaration.provides, declaration.label)),
        targets: definition.inputs.map((declaration) => typeof declaration === "string"
            ? targetPort(node.id, declaration, "any")
            : targetPort(node.id, declaration.id, declaration.accepts, declaration.label)),
    };
}

export function connectGraphPorts(
    first: GraphPortRef,
    second: GraphPortRef,
    nodes: readonly CanvasNodeData[],
    connections: readonly CanvasConnection[],
    connectionId: string,
    registry: Pick<NodeRegistry, "getNode"> = nodeRegistry,
): GraphConnectionResult {
    const sourceCandidate = first.direction === "source" ? first : second.direction === "source" ? second : null;
    const targetCandidate = first.direction === "target" ? first : second.direction === "target" ? second : null;
    if (!sourceCandidate || !targetCandidate) return { ok: false, reason: "incompatible" };
    if (sourceCandidate.nodeId === targetCandidate.nodeId) return { ok: false, reason: "self" };

    const sourceNode = nodes.find((node) => node.id === sourceCandidate.nodeId);
    const targetNode = nodes.find((node) => node.id === targetCandidate.nodeId);
    if (!sourceNode || !targetNode) return { ok: false, reason: "incompatible" };
    const source = getNodePorts(sourceNode, registry).sources.find((candidate) => candidate.portId === sourceCandidate.portId);
    const target = getNodePorts(targetNode, registry).targets.find((candidate) => candidate.portId === targetCandidate.portId);
    if (!source || !target || !portsAreCompatible(source, target, sourceNode, targetNode)) return { ok: false, reason: "incompatible" };

    const duplicate = connections.some((connection) => connection.fromNodeId === source.nodeId
        && connection.fromPortId === source.portId
        && connection.toNodeId === target.nodeId
        && connection.toPortId === target.portId);
    if (duplicate) return { ok: false, reason: "duplicate" };
    if (target.portId === GRAPH_PORT_IDS.prompt && resolveActiveConnections(connections, nodes, registry).some(({ connection, active }) => active
        && connection.toNodeId === target.nodeId && connection.toPortId === GRAPH_PORT_IDS.prompt)) {
        return { ok: false, reason: "prompt-occupied" };
    }
    return {
        ok: true,
        connection: {
            id: connectionId,
            fromNodeId: source.nodeId,
            fromPortId: source.portId,
            toNodeId: target.nodeId,
            toPortId: target.portId,
        },
    };
}

export function resolveActiveConnections(
    connections: readonly CanvasConnection[],
    nodes: readonly CanvasNodeData[],
    registry: Pick<NodeRegistry, "getNode"> = nodeRegistry,
): ResolvedConnectionState[] {
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const claimedPromptTargets = new Set<string>();
    const seenIds = new Set<string>();
    const seenTuples = new Set<string>();
    return connections.map((connection, index) => {
        const connectionKey = graphConnectionTransientKey(connection, index);
        const tuple = JSON.stringify([connection.fromNodeId, connection.fromPortId, connection.toNodeId, connection.toPortId]);
        const duplicateId = seenIds.has(connection.id);
        const duplicateTuple = seenTuples.has(tuple);
        seenIds.add(connection.id);
        seenTuples.add(tuple);
        if (duplicateId) return { connection, connectionKey, active: false, reason: "duplicate-id" };
        if (duplicateTuple) return { connection, connectionKey, active: false, reason: "duplicate" };
        const sourceNode = byId.get(connection.fromNodeId);
        const targetNode = byId.get(connection.toNodeId);
        if (!sourceNode || !targetNode || sourceNode.id === targetNode.id) return { connection, connectionKey, active: false, reason: "missing-node" };
        const source = getNodePorts(sourceNode, registry).sources.find((port) => port.portId === connection.fromPortId);
        const target = getNodePorts(targetNode, registry).targets.find((port) => port.portId === connection.toPortId);
        if (!source || !target) {
            const unresolvedNode = !source ? sourceNode : targetNode;
            const unavailablePlugin = !unresolvedNode.metadata?.graph && !registry.getNode(String(unresolvedNode.type));
            return { connection, connectionKey, active: false, reason: unavailablePlugin ? "opaque" : "missing-port" };
        }
        if (!portsAreCompatible(source, target, sourceNode, targetNode)) return { connection, connectionKey, active: false, reason: "incompatible" };
        if (target.portId === GRAPH_PORT_IDS.prompt) {
            if (claimedPromptTargets.has(target.nodeId)) return { connection, connectionKey, active: false, reason: "prompt-conflict" };
            claimedPromptTargets.add(target.nodeId);
        }
        return { connection, connectionKey, active: true };
    });
}

export function isResolvedGraphConnectionValid(
    connection: CanvasConnection,
    nodes: readonly CanvasNodeData[],
    registry: Pick<NodeRegistry, "getNode">,
) {
    const sourceNode = nodes.find((node) => node.id === connection.fromNodeId);
    const targetNode = nodes.find((node) => node.id === connection.toNodeId);
    if (!sourceNode || !targetNode || sourceNode.id === targetNode.id) return false;
    const source = getNodePorts(sourceNode, registry).sources.find((port) => port.portId === connection.fromPortId);
    const target = getNodePorts(targetNode, registry).targets.find((port) => port.portId === connection.toPortId);
    return Boolean(source && target && portsAreCompatible(source, target, sourceNode, targetNode));
}

export function graphPortDisplayLabel(portId: string, declaredLabel?: string) {
    if (declaredLabel?.trim()) return declaredLabel.trim();
    const labels: Readonly<Record<string, string>> = {
        prompt: "提示词", reference_images: "参考图", first_frame: "首帧", last_frame: "尾帧",
        reference_video: "参考视频", reference_audio: "参考音频", result: "结果", media: "媒体",
    };
    return labels[portId] ?? portId;
}

function sourcePort(nodeId: string, portId: string, valueType: GraphPortValueType, declaredLabel?: string): GraphPortRef {
    return { nodeId, portId, direction: "source", valueType, label: graphPortDisplayLabel(portId, declaredLabel) };
}

function targetPort(nodeId: string, portId: string, valueType: GraphPortValueType, declaredLabel?: string): GraphPortRef {
    return { nodeId, portId, direction: "target", valueType, label: graphPortDisplayLabel(portId, declaredLabel) };
}

function portsAreCompatible(source: GraphPortRef, target: GraphPortRef, sourceNode: CanvasNodeData, targetNode: CanvasNodeData) {
    const targetGraph = targetNode.metadata?.graph;
    if (targetGraph?.role === "result" && target.portId === targetGraph.inputPortId) {
        const sourceGraph = sourceNode.metadata?.graph;
        return (sourceGraph?.role === "model" || sourceGraph?.role === "comfy-workflow") && source.portId === sourceGraph.outputPortId && source.valueType === "result" && target.valueType === "result";
    }
    const standard = targetGraph?.role === "model" || targetNode.type === "config" ? STANDARD_MODEL_INPUT_PORTS[target.portId] : undefined;
    if (standard) return source.valueType === standard.accepts;
    return source.valueType === "any" || target.valueType === "any" || source.valueType === target.valueType;
}
