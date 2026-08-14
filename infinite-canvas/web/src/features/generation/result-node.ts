import { nanoid } from "nanoid";
import { safeApiPath } from "@/api/client";
import type { JobState } from "@/api/contracts";
import { GRAPH_SCHEMA_VERSION } from "@/features/graph/contracts";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from "@/types/canvas";

export function createResultNode(job: JobState, source?: CanvasNodeData, resultIndex = 0, createId: () => string = nanoid): CanvasNodeData {
    const declared = job.results?.[resultIndex];
    const url = declared?.url ?? (resultIndex === 0 ? job.result_url : undefined);
    if (!url) throw new Error("A successful job requires a result URL");
    const isVideo = declared?.media_type === "video" || (!declared && (job.operation?.startsWith("video.") ?? false));
    return {
        id: createId(),
        type: isVideo ? CanvasNodeType.Video : CanvasNodeType.Image,
        title: isVideo ? "生成视频" : "生成图片",
        position: source ? { x: source.position.x + 48 + resultIndex * 28, y: source.position.y + 48 + resultIndex * 28 } : { x: 80 + resultIndex * 28, y: 80 + resultIndex * 28 },
        width: isVideo ? 420 : 340,
        height: isVideo ? 236 : 240,
        metadata: {
            content: safeApiPath(url),
            status: "success",
            sourceJobId: job.id,
            sourceResultIndex: resultIndex,
            graph: {
                schemaVersion: GRAPH_SCHEMA_VERSION,
                role: "result",
                mediaType: isVideo ? "video" : "image",
                inputPortId: "result",
                outputPortId: "media",
                jobId: job.id,
                assetId: declared?.asset_id,
            },
        },
    };
}

/** Idempotent across refresh, concurrent resume and React StrictMode. */
export function appendResultNode(nodes: readonly CanvasNodeData[], job: JobState, source?: CanvasNodeData) {
    return appendJobResults(nodes, [], job, source).nodes;
}

export function appendJobResults(
    nodes: readonly CanvasNodeData[],
    connections: readonly CanvasConnection[],
    job: JobState,
    source?: CanvasNodeData,
    createId: () => string = nanoid,
) {
    const count = job.results?.length || (job.result_url ? 1 : 0);
    const nextNodes = [...nodes];
    const nextConnections = [...connections];
    for (let index = 0; index < count; index += 1) {
        let result = nextNodes.find((node) => node.metadata?.sourceJobId === job.id && (node.metadata.sourceResultIndex ?? 0) === index);
        if (!result) {
            result = createResultNode(job, source, index, createId);
            nextNodes.push(result);
        }
        const sourceGraph = source?.metadata?.graph;
        const resultGraph = result.metadata?.graph;
        if (!source || sourceGraph?.role !== "model" || resultGraph?.role !== "result") continue;
        const exists = nextConnections.some((connection) => connection.fromNodeId === source.id
            && connection.fromPortId === sourceGraph.outputPortId
            && connection.toNodeId === result!.id
            && connection.toPortId === resultGraph.inputPortId);
        if (!exists) nextConnections.push({ id: createId(), fromNodeId: source.id, fromPortId: sourceGraph.outputPortId, toNodeId: result.id, toPortId: resultGraph.inputPortId });
    }
    return { nodes: nextNodes, connections: nextConnections };
}
