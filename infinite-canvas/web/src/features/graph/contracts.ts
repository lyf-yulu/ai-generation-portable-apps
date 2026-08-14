export const GRAPH_SCHEMA_VERSION = 1 as const;

export type GraphParameterValue = string | number | boolean | null;
export type GraphMediaType = "image" | "video" | "audio";
export type GraphNodeRole = "prompt" | "media-collection" | "model" | "result";
export type GraphPortValueType = "prompt" | GraphMediaType | "result" | "any";

export type GraphInputPortDescriptor = {
    id: string;
    accepts: GraphPortValueType;
    label?: string;
};

export type GraphOutputPortDescriptor = {
    id: string;
    provides: GraphPortValueType;
    label?: string;
};

const standardModelInputPorts = {
    prompt: { id: "prompt", accepts: "prompt" },
    reference_images: { id: "reference_images", accepts: "image" },
    first_frame: { id: "first_frame", accepts: "image" },
    last_frame: { id: "last_frame", accepts: "image" },
    reference_video: { id: "reference_video", accepts: "video" },
    reference_audio: { id: "reference_audio", accepts: "audio" },
} as const satisfies Record<string, GraphInputPortDescriptor>;

export const STANDARD_MODEL_INPUT_PORTS: Readonly<Record<string, Readonly<GraphInputPortDescriptor>>> = Object.freeze(
    Object.fromEntries(Object.entries(standardModelInputPorts).map(([id, descriptor]) => [id, Object.freeze({ ...descriptor })])),
);

export function graphInputPortDescriptor(portId: string): GraphInputPortDescriptor {
    const standard = STANDARD_MODEL_INPUT_PORTS[portId];
    return standard ? { ...standard } : { id: portId, accepts: "any" };
}

export type GraphMediaItem = Readonly<{
    id: string;
    assetId: string;
    displayName: string;
    mimeType: string;
    bytes: number;
    width?: number;
    height?: number;
    durationMs?: number;
}>;

export type GraphPromptMetadata = {
    schemaVersion: typeof GRAPH_SCHEMA_VERSION;
    role: "prompt";
    text: string;
    outputPortId: string;
};

export type GraphMediaCollectionMetadata = {
    schemaVersion: typeof GRAPH_SCHEMA_VERSION;
    role: "media-collection";
    mediaType: GraphMediaType;
    outputPortId: string;
    items: GraphMediaItem[];
};

export type GraphModelMetadata = {
    schemaVersion: typeof GRAPH_SCHEMA_VERSION;
    role: "model";
    modelId: string;
    operation: string;
    inputPorts: GraphInputPortDescriptor[];
    outputPortId: string;
    parameters: Record<string, GraphParameterValue>;
};

export type GraphResultMetadata = {
    schemaVersion: typeof GRAPH_SCHEMA_VERSION;
    role: "result";
    mediaType: GraphMediaType;
    inputPortId: string;
    outputPortId: string;
    assetId?: string;
    jobId?: string;
};

export const MAX_GRAPH_PORTS = 32;
export const SAFE_GRAPH_PORT_ID = /^[A-Za-z][A-Za-z0-9._:-]{0,63}$/;

export class InvalidGraphPortDeclarationError extends TypeError {
    constructor() {
        super("Invalid graph port declaration");
        this.name = "InvalidGraphPortDeclarationError";
    }
}

export function assertSafeGraphPortId(id: unknown): asserts id is string {
    if (typeof id !== "string" || !SAFE_GRAPH_PORT_ID.test(id)) throw new InvalidGraphPortDeclarationError();
}

export function assertSafeGraphInputPorts(ports: unknown): asserts ports is GraphInputPortDescriptor[] {
    if (!Array.isArray(ports) || ports.length > MAX_GRAPH_PORTS) throw new InvalidGraphPortDeclarationError();
    const ids = new Set<string>();
    for (const port of ports) {
        if (!port || typeof port !== "object") throw new InvalidGraphPortDeclarationError();
        const descriptor = port as Record<string, unknown>;
        assertSafeGraphPortId(descriptor.id);
        const label = descriptor.label;
        const invalidLabel = label !== undefined && (typeof label !== "string" || label.length === 0 || label.length > 64 || /[\u0000-\u001f\u007f]/.test(label));
        if (ids.has(descriptor.id) || !isGraphPortValueType(descriptor.accepts) || invalidLabel) throw new InvalidGraphPortDeclarationError();
        ids.add(descriptor.id);
    }
}

export function assertSafeLegacyGraphInputPortIds(ports: unknown): asserts ports is string[] {
    if (!Array.isArray(ports) || !ports.every((portId) => typeof portId === "string")) throw new InvalidGraphPortDeclarationError();
    assertSafeGraphInputPorts(ports.map(graphInputPortDescriptor));
}

export function isGraphPortValueType(value: unknown): value is GraphPortValueType {
    return value === "prompt" || value === "image" || value === "video" || value === "audio" || value === "result" || value === "any";
}

export type CanvasGraphNodeMetadata = GraphPromptMetadata | GraphMediaCollectionMetadata | GraphModelMetadata | GraphResultMetadata;

export type GraphSubmissionInput = Readonly<{
    portId: string;
    mediaType: GraphMediaType;
    assetIds: readonly string[];
}>;

export type GraphSubmissionSnapshot = Readonly<{
    schemaVersion: typeof GRAPH_SCHEMA_VERSION;
    prompt: string;
    modelId: string;
    operation: string;
    parameters: Readonly<Record<string, GraphParameterValue>>;
    inputs: readonly GraphSubmissionInput[];
}>;

export type GraphSubmissionSnapshotSource = Omit<GraphSubmissionSnapshot, "schemaVersion">;

export function createGraphSubmissionSnapshot(source: GraphSubmissionSnapshotSource): GraphSubmissionSnapshot {
    const parameters = Object.freeze({ ...source.parameters });
    const inputs = Object.freeze(source.inputs.map((input) => Object.freeze({
        portId: input.portId,
        mediaType: input.mediaType,
        assetIds: Object.freeze([...input.assetIds]),
    })));
    return Object.freeze({
        schemaVersion: GRAPH_SCHEMA_VERSION,
        prompt: source.prompt,
        modelId: source.modelId,
        operation: source.operation,
        parameters,
        inputs,
    });
}
