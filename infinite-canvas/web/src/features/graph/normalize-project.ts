import { GRAPH_SCHEMA_VERSION, STANDARD_MODEL_INPUT_PORTS, assertSafeGraphInputPorts, assertSafeGraphPortId, assertSafeLegacyGraphInputPortIds, deriveResultAssetId, graphInputPortDescriptor, isGraphNodeRole, isGraphPortValueType, type CanvasGraphNodeMetadata, type GraphInputPortDescriptor, type GraphMediaType, type GraphParameterValue, type GraphPortValueType } from "@/features/graph/contracts";
import type { NodeDefinition } from "@/features/nodes/types";
import type { CanvasProject } from "@/stores/canvas/use-canvas-store";
import { normalizeNodeScale } from "@/lib/canvas/node-scale";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData, type CanvasNodeMetadata } from "@/types/canvas";

export class UnsupportedGraphSchemaError extends Error {
    constructor(version: unknown) {
        super(`Unsupported canvas graph schema version: ${version}`);
        this.name = "UnsupportedGraphSchemaError";
    }
}

export type CanvasConnectionInput = Omit<CanvasConnection, "fromPortId" | "toPortId"> & {
    fromPortId?: string;
    toPortId?: string;
};

export type CanvasNodeInput = Omit<CanvasNodeData, "metadata"> & {
    metadata?: Omit<CanvasNodeMetadata, "graph"> & { graph?: unknown };
};

export type CanvasProjectInput = Omit<CanvasProject, "graphSchemaVersion" | "nodes" | "connections"> & {
    graphSchemaVersion?: number;
    nodes: CanvasNodeInput[];
    connections: CanvasConnectionInput[];
};

export type CanvasNodePortResolver = {
    getNode: (id: string) => Pick<NodeDefinition, "inputs" | "outputs"> | undefined;
};

export function normalizeCanvasProject(project: CanvasProjectInput, portResolver?: CanvasNodePortResolver): CanvasProject {
    const cloned = cloneJsonValue(project) as CanvasProjectInput;
    assertSupportedSchema(cloned);
    const nodes = cloned.nodes.map(normalizeNode);
    return {
        ...cloned,
        graphSchemaVersion: GRAPH_SCHEMA_VERSION,
        nodes,
        connections: normalizeConnections(cloned.connections, nodes, portResolver),
        chatSessions: cloned.chatSessions,
        viewport: cloned.viewport,
    };
}

export function normalizeCanvasProjectBaselineSnapshot(snapshot: string, fallbackProject: CanvasProjectInput, portResolver?: CanvasNodePortResolver): string {
    const parsed = JSON.parse(snapshot) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new TypeError("Invalid canvas project baseline snapshot");
    const normalized = normalizeCanvasProject({ ...fallbackProject, ...(parsed as Partial<CanvasProjectInput>), updatedAt: fallbackProject.updatedAt }, portResolver);
    const { updatedAt: _timestamp, ...content } = normalized;
    return canonicalJson(content);
}

function assertSupportedSchema(project: CanvasProjectInput) {
    if (Object.prototype.hasOwnProperty.call(project, "graphSchemaVersion")) assertCurrentSchemaVersion(project.graphSchemaVersion);
    for (const node of project.nodes) {
        const graph = node.metadata?.graph;
        if (!graph || typeof graph !== "object") continue;
        if (Object.prototype.hasOwnProperty.call(graph, "schemaVersion")) assertCurrentSchemaVersion((graph as { schemaVersion?: unknown }).schemaVersion);
        const candidate = graph as Record<string, unknown>;
        if (candidate.schemaVersion === GRAPH_SCHEMA_VERSION && (candidate.role === "model" || candidate.role === "comfy-workflow")) {
            if (Object.prototype.hasOwnProperty.call(candidate, "inputPorts")) {
                assertSafeGraphInputPorts(candidate.inputPorts);
                assertSafeGraphPortId(candidate.outputPortId);
            } else if (Object.prototype.hasOwnProperty.call(candidate, "inputPortIds")) {
                assertSafeLegacyGraphInputPortIds(candidate.inputPortIds);
                assertSafeGraphPortId(candidate.outputPortId);
            }
        }
        if (candidate.schemaVersion === GRAPH_SCHEMA_VERSION && (candidate.role === "prompt" || candidate.role === "media-collection" || candidate.role === "result")) {
            if (Object.prototype.hasOwnProperty.call(candidate, "outputPortId")) assertSafeGraphPortId(candidate.outputPortId);
            if (candidate.role === "result" && Object.prototype.hasOwnProperty.call(candidate, "inputPortId")) assertSafeGraphPortId(candidate.inputPortId);
        }
        if (candidate.schemaVersion === GRAPH_SCHEMA_VERSION && candidate.role === "comfy-workflow") {
            assertSafeGraphPortId(candidate.workflowId);
            if (!Number.isInteger(candidate.workflowRevision) || (candidate.workflowRevision as number) < 1 || candidate.executionEnabled !== false) throw new TypeError("Invalid ComfyUI workflow metadata");
        }
    }
}

function assertCurrentSchemaVersion(version: unknown) {
    if (typeof version !== "number" || !Number.isInteger(version) || version !== GRAPH_SCHEMA_VERSION) throw new UnsupportedGraphSchemaError(version);
}

function cloneJsonValue(value: unknown, depth = 0, budget = { remaining: 100_000 }): unknown {
    if (depth > 64 || budget.remaining-- <= 0) throw new TypeError("Canvas project JSON exceeds clone bounds");
    if (value === null || value === undefined || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number") {
        if (!Number.isFinite(value)) throw new TypeError("Canvas project JSON contains a non-finite number");
        return value;
    }
    if (typeof value !== "object") throw new TypeError("Canvas project contains a non-JSON value");
    if (Array.isArray(value)) {
        const result: unknown[] = new Array(value.length);
        for (let index = 0; index < value.length; index += 1) {
            const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
            if (!descriptor) continue;
            if (!("value" in descriptor)) throw new TypeError("Canvas project contains an accessor");
            result[index] = cloneJsonValue(descriptor.value, depth + 1, budget);
        }
        return result;
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) throw new TypeError("Canvas project contains a non-JSON object");
    const result: Record<string, unknown> = {};
    for (const [key, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(value))) {
        if (!descriptor.enumerable) continue;
        if (!("value" in descriptor)) throw new TypeError("Canvas project contains an accessor");
        Object.defineProperty(result, key, {
            value: cloneJsonValue(descriptor.value, depth + 1, budget),
            enumerable: true,
            configurable: true,
            writable: true,
        });
    }
    return result;
}

function canonicalJson(value: unknown): string {
    const sort = (item: unknown): unknown => {
        if (Array.isArray(item)) return item.map(sort);
        if (!item || typeof item !== "object") return item;
        return Object.fromEntries(Object.entries(item)
            .sort(([left], [right]) => left.localeCompare(right))
            .map(([key, child]) => [key, sort(child)]));
    };
    return JSON.stringify(sort(value));
}

function normalizeNode(node: CanvasNodeInput): CanvasNodeData {
    const metadata = node.metadata ? { ...node.metadata } : undefined;
    const { scale: rawScale, ...rest } = node;
    const scale = normalizeNodeScale(rawScale);
    const base = { ...rest, position: { ...node.position }, ...(scale === undefined ? {} : { scale }) };
    if (!isBuiltInGraphNode(node.type)) return { ...base, metadata: metadata as CanvasNodeMetadata | undefined };
    const graph = normalizeGraphMetadata(node, metadata);
    if (node.type === "comfy.workflow") {
        // A later execution slice must re-authorize this ID/revision server-side; local project data never proves assignment.
        return { ...base, metadata: { status: safeCanvasNodeStatus(metadata?.status), graph } };
    }
    return {
        ...base,
        metadata: { ...metadata, graph },
    };
}

function safeCanvasNodeStatus(value: unknown): CanvasNodeMetadata["status"] {
    return value === "success" || value === "loading" || value === "error" ? value : "idle";
}

function isBuiltInGraphNode(type: CanvasNodeData["type"]) {
    return type === CanvasNodeType.Text || type === CanvasNodeType.Config || type === CanvasNodeType.Image || type === CanvasNodeType.Video || type === CanvasNodeType.Audio || type === "comfy.workflow";
}

function normalizeGraphMetadata(node: CanvasNodeInput, metadata?: CanvasNodeInput["metadata"]): CanvasGraphNodeMetadata {
    if (isCurrentGraphMetadata(metadata?.graph)) return cloneGraphMetadata(metadata.graph);
    if (isLegacyGraphModelMetadata(metadata?.graph)) {
        return {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "model",
            modelId: metadata.graph.modelId,
            operation: metadata.graph.operation,
            inputPorts: metadata.graph.inputPortIds.map(graphInputPortDescriptor),
            outputPortId: metadata.graph.outputPortId,
            parameters: pruneModelParameters(metadata.graph.modelId, { ...metadata.graph.parameters }),
        };
    }
    if (isLegacyGraphResultMetadata(metadata?.graph)) {
        const assetId = metadata.graph.assetId ?? deriveResultAssetId(metadata);
        return {
            ...metadata.graph,
            inputPortId: "result",
            ...(assetId ? { assetId } : {}),
        };
    }
    if (node.type === CanvasNodeType.Text) {
        return {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "prompt",
            text: metadata?.content ?? metadata?.composerContent ?? metadata?.prompt ?? "",
            outputPortId: "prompt",
        };
    }
    if (node.type === CanvasNodeType.Config) {
        return {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "model",
            modelId: metadata?.model ?? "",
            operation: inferLegacyOperation(metadata),
            inputPorts: ["prompt", "reference_images", "first_frame", "last_frame", "reference_video", "reference_audio"].map(graphInputPortDescriptor),
            outputPortId: "result",
            parameters: scalarParameters(metadata?.params),
        };
    }
    if (node.type === "comfy.workflow") return {
        schemaVersion: GRAPH_SCHEMA_VERSION,
        role: "comfy-workflow",
        workflowId: "unassigned",
        workflowRevision: 1,
        inputPorts: [],
        outputPortId: "result",
        executionEnabled: false,
    };
    const mediaType = mediaTypeForNode(node.type) ?? "image";
    const assetId = deriveResultAssetId(metadata);
    return {
        schemaVersion: GRAPH_SCHEMA_VERSION,
        role: "result",
        mediaType,
        inputPortId: "result",
        outputPortId: "media",
        ...(metadata?.sourceJobId ? { jobId: metadata.sourceJobId } : {}),
        ...(assetId ? { assetId } : {}),
    };
}

function isCurrentGraphMetadata(value: unknown): value is CanvasGraphNodeMetadata {
    if (!value || typeof value !== "object") return false;
    const candidate = value as Record<string, unknown>;
    if (candidate.schemaVersion !== GRAPH_SCHEMA_VERSION) return false;
    if (!isGraphNodeRole(candidate.role)) return false;
    if (candidate.role === "prompt") return typeof candidate.text === "string" && typeof candidate.outputPortId === "string";
    if (candidate.role === "media-collection") {
        return isMediaType(candidate.mediaType)
            && typeof candidate.outputPortId === "string"
            && Array.isArray(candidate.items)
            && candidate.items.every(isGraphMediaItem);
    }
    if (candidate.role === "model") {
        return typeof candidate.modelId === "string"
            && typeof candidate.operation === "string"
            && typeof candidate.outputPortId === "string"
            && Array.isArray(candidate.inputPorts)
            && candidate.inputPorts.every(isGraphInputPortDescriptor)
            && isParameterRecord(candidate.parameters);
    }
    if (candidate.role === "comfy-workflow") {
        return typeof candidate.workflowId === "string"
            && typeof candidate.workflowRevision === "number"
            && Number.isInteger(candidate.workflowRevision)
            && candidate.workflowRevision > 0
            && typeof candidate.outputPortId === "string"
            && Array.isArray(candidate.inputPorts)
            && candidate.inputPorts.every(isGraphInputPortDescriptor)
            && candidate.executionEnabled === false;
    }
    if (candidate.role === "result") {
        return isMediaType(candidate.mediaType)
            && typeof candidate.inputPortId === "string"
            && typeof candidate.outputPortId === "string"
            && (candidate.assetId === undefined || typeof candidate.assetId === "string")
            && (candidate.jobId === undefined || typeof candidate.jobId === "string");
    }
    return false;
}

function isLegacyGraphModelMetadata(value: unknown): value is {
    schemaVersion: typeof GRAPH_SCHEMA_VERSION;
    role: "model";
    modelId: string;
    operation: string;
    inputPortIds: string[];
    outputPortId: string;
    parameters: Record<string, GraphParameterValue>;
} {
    if (!value || typeof value !== "object") return false;
    const candidate = value as Record<string, unknown>;
    return candidate.schemaVersion === GRAPH_SCHEMA_VERSION
        && candidate.role === "model"
        && typeof candidate.modelId === "string"
        && typeof candidate.operation === "string"
        && typeof candidate.outputPortId === "string"
        && Array.isArray(candidate.inputPortIds)
        && candidate.inputPortIds.every((port) => typeof port === "string")
        && isParameterRecord(candidate.parameters);
}

function isLegacyGraphResultMetadata(value: unknown): value is Omit<Extract<CanvasGraphNodeMetadata, { role: "result" }>, "inputPortId"> {
    if (!value || typeof value !== "object") return false;
    const candidate = value as Record<string, unknown>;
    return candidate.schemaVersion === GRAPH_SCHEMA_VERSION
        && candidate.role === "result"
        && isMediaType(candidate.mediaType)
        && typeof candidate.outputPortId === "string"
        && !Object.prototype.hasOwnProperty.call(candidate, "inputPortId")
        && (candidate.assetId === undefined || typeof candidate.assetId === "string")
        && (candidate.jobId === undefined || typeof candidate.jobId === "string");
}

function isGraphInputPortDescriptor(value: unknown): value is GraphInputPortDescriptor {
    if (!value || typeof value !== "object") return false;
    const descriptor = value as Record<string, unknown>;
    return typeof descriptor.id === "string" && descriptor.id.length > 0 && isGraphPortValueType(descriptor.accepts);
}

function isMediaType(value: unknown): value is GraphMediaType {
    return value === "image" || value === "video" || value === "audio";
}

function isGraphMediaItem(value: unknown) {
    if (!value || typeof value !== "object") return false;
    const item = value as Record<string, unknown>;
    return typeof item.id === "string"
        && typeof item.assetId === "string"
        && typeof item.displayName === "string"
        && typeof item.mimeType === "string"
        && isFiniteNonNegative(item.bytes)
        && (item.kind === undefined || item.kind === "library")
        && (item.width === undefined || isFiniteNonNegative(item.width))
        && (item.height === undefined || isFiniteNonNegative(item.height))
        && (item.durationMs === undefined || isFiniteNonNegative(item.durationMs));
}

function isFiniteNonNegative(value: unknown) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isParameterRecord(value: unknown): value is Record<string, GraphParameterValue> {
    return Boolean(value && typeof value === "object" && !Array.isArray(value) && Object.values(value).every((item) => item === null || typeof item === "string" || typeof item === "boolean" || (typeof item === "number" && Number.isFinite(item))));
}

const KNOWN_MODEL_PARAMETERS: Readonly<Record<string, ReadonlySet<string>>> = {
    "demo-image-v1": new Set(["size", "ratio"]),
};

function pruneModelParameters(modelId: string, parameters: Record<string, GraphParameterValue>): Record<string, GraphParameterValue> {
    const allowed = KNOWN_MODEL_PARAMETERS[modelId];
    if (!allowed) return { ...parameters };
    return Object.fromEntries(Object.entries(parameters).filter(([name]) => allowed.has(name)));
}

function cloneGraphMetadata(metadata: CanvasGraphNodeMetadata): CanvasGraphNodeMetadata {
    if (metadata.role === "media-collection") return { ...metadata, items: metadata.items.map((item) => ({ ...item })) };
    if (metadata.role === "model") return {
        schemaVersion: GRAPH_SCHEMA_VERSION,
        role: "model",
        modelId: metadata.modelId,
        operation: metadata.operation,
        inputPorts: metadata.inputPorts.map(projectGraphInputPortDescriptor),
        outputPortId: metadata.outputPortId,
        parameters: pruneModelParameters(metadata.modelId, metadata.parameters),
    };
    if (metadata.role === "comfy-workflow") return {
        schemaVersion: GRAPH_SCHEMA_VERSION,
        role: "comfy-workflow",
        workflowId: metadata.workflowId,
        workflowRevision: metadata.workflowRevision,
        inputPorts: metadata.inputPorts.map(projectGraphInputPortDescriptor),
        outputPortId: metadata.outputPortId,
        executionEnabled: false,
    };
    return { ...metadata };
}

function projectGraphInputPortDescriptor(port: GraphInputPortDescriptor): GraphInputPortDescriptor {
    const label = typeof port.label === "string" && port.label.length > 0 && port.label.length <= 64 && !/[\u0000-\u001f\u007f]/.test(port.label) ? port.label : undefined;
    return { id: port.id, accepts: port.accepts, ...(label === undefined ? {} : { label }) };
}

function inferLegacyOperation(metadata?: CanvasNodeInput["metadata"]) {
    if (metadata?.generationMode === "video") return "video.generate";
    if (metadata?.generationMode === "audio") return "audio.generate";
    if (metadata?.generationType === "edit") return "image.edit";
    return "image.generate";
}

function scalarParameters(value: unknown): Record<string, GraphParameterValue> {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, GraphParameterValue] => {
        const item = entry[1];
        return item === null || typeof item === "string" || typeof item === "boolean" || (typeof item === "number" && Number.isFinite(item));
    }));
}

function mediaTypeForNode(type: CanvasNodeData["type"]): GraphMediaType | null {
    if (type === CanvasNodeType.Image) return "image";
    if (type === CanvasNodeType.Video) return "video";
    if (type === CanvasNodeType.Audio) return "audio";
    return null;
}

function normalizeConnections(connections: CanvasConnectionInput[], nodes: CanvasNodeData[], portResolver?: CanvasNodePortResolver): CanvasConnection[] {
    void portResolver;
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const normalized: CanvasConnection[] = [];
    for (const connection of connections) {
        const from = byId.get(connection.fromNodeId);
        const to = byId.get(connection.toNodeId);
        if (!from || !to || from.id === to.id) continue;
        const hasFromPort = Object.prototype.hasOwnProperty.call(connection, "fromPortId");
        const hasToPort = Object.prototype.hasOwnProperty.call(connection, "toPortId");
        if (hasFromPort !== hasToPort) continue;
        const explicit = hasFromPort && hasToPort;
        if (explicit && (!connection.fromPortId || !connection.toPortId)) continue;
        const ports = explicit
            ? { fromPortId: connection.fromPortId!, toPortId: connection.toPortId! }
            : inferLegacyPorts(from, to);
        if (!ports) continue;
        const validity = explicit ? classifyExplicitConnection(from, ports.fromPortId, to, ports.toPortId) : "valid";
        if (validity === "invalid") continue;
        normalized.push({ id: connection.id, fromNodeId: from.id, fromPortId: ports.fromPortId, toNodeId: to.id, toPortId: ports.toPortId });
    }
    return normalized;
}

type ConnectionValidity = "valid" | "invalid" | "opaque";

function classifyExplicitConnection(from: CanvasNodeData, fromPortId: string, to: CanvasNodeData, toPortId: string): ConnectionValidity {
    if (!isBuiltInGraphNode(from.type) || !isBuiltInGraphNode(to.type)) return "opaque";
    const source = from.metadata?.graph;
    const target = to.metadata?.graph;
    const sourceType = resolveSourceType(from, fromPortId);
    if (sourceType === "invalid") return "invalid";
    if (!target) {
        return "invalid";
    }
    if (target.role === "model" || target.role === "comfy-workflow") {
        const input = target.inputPorts.find((port) => port.id === toPortId);
        if (!input) return "invalid";
        if (sourceType === "unknown") return "opaque";
        const standard = STANDARD_MODEL_INPUT_PORTS[toPortId];
        if (standard) return sourceType === standard.accepts ? "valid" : "invalid";
        return portTypesMatch(sourceType, input.accepts) ? "valid" : "invalid";
    }
    if (target.role === "result" && toPortId === target.inputPortId && (source?.role === "model" || source?.role === "comfy-workflow") && fromPortId === source.outputPortId) return "valid";
    return "invalid";
}

function resolveSourceType(node: CanvasNodeData, portId: string, portResolver?: CanvasNodePortResolver): GraphPortValueType | "invalid" | "unknown" {
    const graph = node.metadata?.graph;
    if (graph) {
        if (portId !== graph.outputPortId) return "invalid";
        return graphSourceValueType(graph);
    }
    const definition = portResolver?.getNode(String(node.type));
    if (!definition) return "unknown";
    const output = definition.outputs.find((declaration) => (typeof declaration === "string" ? declaration : declaration.id) === portId);
    if (!output) return "invalid";
    return typeof output === "string" ? "any" : output.provides;
}

function portTypesMatch(source: GraphPortValueType, target: GraphPortValueType) {
    return source === "any" || target === "any" || source === target;
}

function graphSourceValueType(metadata: CanvasGraphNodeMetadata): GraphPortValueType {
    if (metadata.role === "prompt") return "prompt";
    if (metadata.role === "media-collection" || metadata.role === "result") return metadata.mediaType;
    return "result";
}

function inferLegacyPorts(from: CanvasNodeData, to: CanvasNodeData) {
    const source = from.metadata?.graph;
    const target = to.metadata?.graph;
    if (source?.role === "prompt" && target?.role === "model") return { fromPortId: "prompt", toPortId: "prompt" };
    if ((source?.role === "media-collection" || source?.role === "result") && target?.role === "model") {
        const toPortId = source.mediaType === "image" ? "reference_images" : source.mediaType === "video" ? "reference_video" : "reference_audio";
        return { fromPortId: "media", toPortId };
    }
    if (source?.role === "model" && target?.role === "result") return { fromPortId: "result", toPortId: "result" };
    return null;
}
