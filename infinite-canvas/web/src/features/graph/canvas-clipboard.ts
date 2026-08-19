import { currentStorageScope, currentStorageScopeVersion, onStorageScopeCleared } from "@/storage/scope";
import type { CanvasProject } from "@/stores/canvas/use-canvas-store";
import type { CanvasConnection, CanvasNodeData, CanvasNodeMetadata } from "@/types/canvas";

const MAX_PROJECT_NODES = 1000;
const MAX_PROJECT_CONNECTIONS = 2000;
const PASTE_OFFSET = 32;

type ClipboardSnapshot = {
    scopeVersion: number;
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    pasteCount: number;
};

export type CopyCanvasSelectionResult = { ok: true; nodeCount: number } | { ok: false; reason: "empty-selection" | "no-scope" };
export type PasteCanvasSelectionResult =
    | { ok: true; nodes: CanvasNodeData[]; connections: CanvasConnection[]; pastedNodeIds: string[] }
    | { ok: false; reason: "empty" | "node-limit" | "connection-limit" };

let clipboard: ClipboardSnapshot | null = null;
onStorageScopeCleared(() => {
    clipboard = null;
});

function cloneJson<T>(value: T): T {
    return JSON.parse(JSON.stringify(value)) as T;
}

export function clearCanvasClipboard() {
    clipboard = null;
}

export function copyCanvasSelection(project: CanvasProject, selectedIds: ReadonlySet<string>): CopyCanvasSelectionResult {
    if (!currentStorageScope()) return { ok: false, reason: "no-scope" };
    const nodes = project.nodes.filter((node) => selectedIds.has(node.id));
    if (nodes.length === 0) return { ok: false, reason: "empty-selection" };
    const copiedIds = new Set(nodes.map((node) => node.id));
    clipboard = {
        scopeVersion: currentStorageScopeVersion(),
        nodes: cloneJson(nodes),
        connections: cloneJson(project.connections.filter((connection) => copiedIds.has(connection.fromNodeId) && copiedIds.has(connection.toNodeId))),
        pasteCount: 0,
    };
    return { ok: true, nodeCount: nodes.length };
}

function remapKnownNodeReferences(metadata: CanvasNodeMetadata | undefined, idMap: ReadonlyMap<string, string>) {
    if (!metadata) return metadata;
    const next = cloneJson(metadata);
    const remap = (value: string | undefined) => (value ? idMap.get(value) : undefined);
    if (next.groupId) next.groupId = remap(next.groupId);
    if (next.batchRootId) next.batchRootId = remap(next.batchRootId);
    if (next.primaryImageId) next.primaryImageId = remap(next.primaryImageId);
    if (next.batchChildIds) next.batchChildIds = next.batchChildIds.flatMap((id) => {
        const mapped = idMap.get(id);
        return mapped ? [mapped] : [];
    });
    delete next.jobId;
    delete next.jobStatus;
    delete next.idempotencyKey;
    delete next.requestId;
    delete next.phase;
    if (next.graph?.role === "model") next.status = "idle";
    return next;
}

export function pasteCanvasSelection(project: CanvasProject, createId: () => string): PasteCanvasSelectionResult {
    const snapshot = clipboard;
    if (!snapshot || !currentStorageScope() || snapshot.scopeVersion !== currentStorageScopeVersion()) {
        clipboard = null;
        return { ok: false, reason: "empty" };
    }
    if (project.nodes.length + snapshot.nodes.length > MAX_PROJECT_NODES) return { ok: false, reason: "node-limit" };
    if (project.connections.length + snapshot.connections.length > MAX_PROJECT_CONNECTIONS) return { ok: false, reason: "connection-limit" };
    snapshot.pasteCount += 1;
    const offset = PASTE_OFFSET * snapshot.pasteCount;
    const idMap = new Map(snapshot.nodes.map((node) => [node.id, createId()]));
    const nodes = snapshot.nodes.map((node) => ({
        ...cloneJson(node),
        id: idMap.get(node.id)!,
        position: { x: node.position.x + offset, y: node.position.y + offset },
        metadata: remapKnownNodeReferences(node.metadata, idMap),
    }));
    const connections = snapshot.connections.map((connection) => ({
        ...cloneJson(connection),
        id: createId(),
        fromNodeId: idMap.get(connection.fromNodeId)!,
        toNodeId: idMap.get(connection.toNodeId)!,
    }));
    return { ok: true, nodes, connections, pastedNodeIds: nodes.map((node) => node.id) };
}
