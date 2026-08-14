import type { CanvasConnection, CanvasNodeData } from "@/types/canvas";

export function selectNode(current: ReadonlySet<string>, nodeId: string, additive: boolean): Set<string> {
    if (!additive) return new Set([nodeId]);
    const next = new Set(current);
    if (next.has(nodeId)) next.delete(nodeId);
    else next.add(nodeId);
    return next;
}

export function deleteGraphNodes(nodes: CanvasNodeData[], connections: CanvasConnection[], nodeIds: ReadonlySet<string>) {
    if (nodeIds.size === 0) return { nodes, connections };
    return {
        nodes: nodes.filter((node) => !nodeIds.has(node.id)),
        connections: connections.filter((connection) => !nodeIds.has(connection.fromNodeId) && !nodeIds.has(connection.toNodeId)),
    };
}

export function isEditableEventTarget(target: EventTarget | null): boolean {
    if (!(target instanceof Element)) return false;
    return Boolean(target.closest("input,textarea,select,[contenteditable]:not([contenteditable='false'])"));
}
