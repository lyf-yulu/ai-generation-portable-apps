import { CanvasNodeType, type CanvasNodeData, type ConnectionHandle } from "@/types/canvas";
import { getNodePorts, type GraphPortRef } from "@/features/graph/connect";

export function nodeBounds(nodes: CanvasNodeData[]) {
    return nodes.reduce(
        (acc, node) => ({
            left: Math.min(acc.left, node.position.x),
            top: Math.min(acc.top, node.position.y),
            right: Math.max(acc.right, node.position.x + node.width),
            bottom: Math.max(acc.bottom, node.position.y + node.height),
        }),
        { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity },
    );
}

export function findGroupDropTarget(movedIds: Set<string>, nodes: CanvasNodeData[]) {
    if (nodes.some((node) => movedIds.has(node.id) && node.type === CanvasNodeType.Group)) return null;
    const movingNodes = nodes.filter((node) => movedIds.has(node.id) && node.type !== CanvasNodeType.Group);
    if (!movingNodes.length) return null;
    return (
        [...nodes].reverse().find((group) => {
            if (group.type !== CanvasNodeType.Group || movedIds.has(group.id)) return false;
            return movingNodes.some((node) => {
                const centerX = node.position.x + node.width / 2;
                const centerY = node.position.y + node.height / 2;
                return centerX >= group.position.x && centerX <= group.position.x + group.width && centerY >= group.position.y && centerY <= group.position.y + group.height;
            });
        }) || null
    );
}

export function snapNodesIntoGroup(movedIds: Set<string>, nodes: CanvasNodeData[], group: CanvasNodeData) {
    const movingNodes = nodes.filter((node) => movedIds.has(node.id) && node.type !== CanvasNodeType.Group);
    if (!movingNodes.length) return nodes;
    const pad = 24;
    const bounds = nodeBounds(movingNodes);
    const left = group.position.x + pad;
    const top = group.position.y + pad;
    const right = group.position.x + group.width - pad;
    const bottom = group.position.y + group.height - pad;
    const dx = bounds.right - bounds.left > right - left ? left - bounds.left : bounds.left < left ? left - bounds.left : bounds.right > right ? right - bounds.right : 0;
    const dy = bounds.bottom - bounds.top > bottom - top ? top - bounds.top : bounds.top < top ? top - bounds.top : bounds.bottom > bottom ? bottom - bounds.bottom : 0;
    return nodes.map((node) => {
        if (!movedIds.has(node.id) || node.type === CanvasNodeType.Group) return node;
        return { ...node, position: { x: node.position.x + dx, y: node.position.y + dy }, metadata: { ...node.metadata, groupId: group.id } };
    });
}

export function findContainingGroupId(node: CanvasNodeData, nodes: CanvasNodeData[]) {
    const centerX = node.position.x + node.width / 2;
    const centerY = node.position.y + node.height / 2;
    return (
        [...nodes]
            .reverse()
            .find((group) => group.type === CanvasNodeType.Group && group.id !== node.id && centerX >= group.position.x && centerX <= group.position.x + group.width && centerY >= group.position.y && centerY <= group.position.y + group.height)?.id ||
        undefined
    );
}

export function getConnectionTargetAnchor(node: CanvasNodeData, current: ConnectionHandle) {
    return {
        x: current.handleType === "source" ? node.position.x : node.position.x + node.width,
        y: node.position.y + node.height / 2,
    };
}

export function getNodePortAnchor(node: CanvasNodeData, portId: string, direction: GraphPortRef["direction"]) {
    const ports = getNodePorts(node);
    const siblings = direction === "source" ? ports.sources : ports.targets;
    const index = Math.max(0, siblings.findIndex((port) => port.portId === portId));
    return {
        x: direction === "source" ? node.position.x + node.width : node.position.x,
        y: node.position.y + ((index + 1) * node.height) / (siblings.length + 1),
    };
}

export function connectionPathData(from: CanvasNodeData, fromPortId: string, to: CanvasNodeData, toPortId: string) {
    const start = getNodePortAnchor(from, fromPortId, "source");
    const end = getNodePortAnchor(to, toPortId, "target");
    const dx = Math.abs(end.x - start.x);
    const curvature = Math.max(dx * 0.5, 50);
    return `M ${start.x} ${start.y} C ${start.x + curvature} ${start.y}, ${end.x - curvature} ${end.y}, ${end.x} ${end.y}`;
}

export function normalizeConnection(firstNodeId: string, secondNodeId: string, nodes: CanvasNodeData[], firstHandleType: "source" | "target") {
    const first = nodes.find((node) => node.id === firstNodeId);
    const second = nodes.find((node) => node.id === secondNodeId);
    if (!first || !second || first.id === second.id) return null;
    if (first.type === CanvasNodeType.Group || second.type === CanvasNodeType.Group) return null;
    if (first.type === CanvasNodeType.Config && second.type === CanvasNodeType.Config) return null;
    const oriented = second.type === CanvasNodeType.Config
        ? { from: first, to: second }
        : first.type === CanvasNodeType.Config && firstHandleType === "target"
          ? { from: second, to: first }
          : { from: first, to: second };
    const source = graphNodeKind(oriented.from);
    const target = graphNodeKind(oriented.to);
    const fromPortId = source.role === "prompt" ? "prompt" : source.role === "model" ? "result" : "media";
    const toPortId = target.role === "model"
        ? source.role === "prompt"
            ? "prompt"
            : source.mediaType === "video"
              ? "reference_video"
              : source.mediaType === "audio"
                ? "reference_audio"
                : "reference_images"
        : "result";
    return { fromNodeId: oriented.from.id, fromPortId, toNodeId: oriented.to.id, toPortId };
}

function graphNodeKind(node: CanvasNodeData): { role: "prompt" | "model" | "media"; mediaType?: "image" | "video" | "audio" } {
    const graph = node.metadata?.graph;
    if (graph?.role === "prompt") return { role: "prompt" };
    if (graph?.role === "model") return { role: "model" };
    if (graph?.role === "media-collection" || graph?.role === "result") return { role: "media", mediaType: graph.mediaType };
    if (node.type === CanvasNodeType.Text) return { role: "prompt" };
    if (node.type === CanvasNodeType.Config) return { role: "model" };
    if (node.type === CanvasNodeType.Video) return { role: "media", mediaType: "video" };
    if (node.type === CanvasNodeType.Audio) return { role: "media", mediaType: "audio" };
    return { role: "media", mediaType: "image" };
}

export function isHiddenBatchChild(node: CanvasNodeData, nodes: CanvasNodeData[], collapsingBatchIds?: Set<string>) {
    const rootId = node.metadata?.batchRootId;
    if (!rootId) return false;
    const root = nodes.find((item) => item.id === rootId);
    if (root && collapsingBatchIds?.has(rootId)) return false;
    return Boolean(root && !root.metadata?.imageBatchExpanded);
}

export function isHiddenBatchConnectionEndpoint(node: CanvasNodeData, nodes: CanvasNodeData[]) {
    const rootId = node.metadata?.batchRootId;
    if (!rootId) return false;
    const root = nodes.find((item) => item.id === rootId);
    return Boolean(root && !root.metadata?.imageBatchExpanded);
}
