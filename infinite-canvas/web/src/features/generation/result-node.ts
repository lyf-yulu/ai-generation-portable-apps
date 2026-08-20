import { nanoid } from "nanoid";
import { safeApiPath } from "@/api/client";
import type { JobState } from "@/api/contracts";
import { GRAPH_SCHEMA_VERSION } from "@/features/graph/contracts";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from "@/types/canvas";

const SIZE_PRESET_SHORT_EDGE: Readonly<Record<string, number>> = { "1k": 1024, "1.5k": 1536, "2k": 2048 };
const DEFAULT_SHORT_EDGE_WITHOUT_SIZE = 512;
const DEFAULT_IMAGE_SIZE = { width: 340, height: 240 };
const DEFAULT_VIDEO_SIZE = { width: 420, height: 236 };

function parseRatio(value: string): [number, number] | undefined {
    const match = /^(\d+)\s*[:：]\s*(\d+)$/.exec(value.trim());
    if (!match) return undefined;
    const width = Number(match[1]);
    const height = Number(match[2]);
    return width >= 1 && height >= 1 ? [width, height] : undefined;
}

function parseDimensionString(value: string): { width: number; height: number } | undefined {
    const match = /^(\d+)\s*[x×]\s*(\d+)$/i.exec(value.trim());
    if (!match) return undefined;
    const width = Number(match[1]);
    const height = Number(match[2]);
    return width >= 1 && height >= 1 ? { width, height } : undefined;
}

function sizeForRatio([width, height]: [number, number], shortEdge: number): { width: number; height: number } {
    return width >= height ? { width: Math.round((shortEdge * width) / height), height: shortEdge } : { width: shortEdge, height: Math.round((shortEdge * height) / width) };
}

/** Base size of a result node at scale 1, derived from the generation settings of the source node. */
function resultNodeSize(source: CanvasNodeData | undefined, isVideo: boolean): { width: number; height: number } {
    const params = source?.metadata?.params ?? {};
    const size = typeof params.size === "string" ? params.size : undefined;
    const ratio = typeof params.ratio === "string" ? params.ratio : undefined;
    const custom = size ? parseDimensionString(size) : undefined;
    if (custom) return custom;
    const shortEdge = size ? SIZE_PRESET_SHORT_EDGE[size.trim().toLowerCase()] : undefined;
    const parsedRatio = ratio ? parseRatio(ratio) : undefined;
    if (shortEdge !== undefined) return parsedRatio ? sizeForRatio(parsedRatio, shortEdge) : { width: shortEdge, height: shortEdge };
    if (parsedRatio) return sizeForRatio(parsedRatio, DEFAULT_SHORT_EDGE_WITHOUT_SIZE);
    return isVideo ? DEFAULT_VIDEO_SIZE : DEFAULT_IMAGE_SIZE;
}

export function createResultNode(job: JobState, source?: CanvasNodeData, resultIndex = 0, createId: () => string = nanoid): CanvasNodeData {
    const declared = job.results?.[resultIndex];
    const url = declared?.url ?? (resultIndex === 0 ? job.result_url : undefined);
    if (!url) throw new Error("A successful job requires a result URL");
    const isVideo = declared?.media_type === "video" || (!declared && (job.operation?.startsWith("video.") ?? false));
    const size = resultNodeSize(source, isVideo);
    return {
        id: createId(),
        type: isVideo ? CanvasNodeType.Video : CanvasNodeType.Image,
        title: isVideo ? "生成视频" : "生成图片",
        position: source ? { x: source.position.x + source.width + 48 + resultIndex * 28, y: source.position.y + resultIndex * 28 } : { x: 80 + resultIndex * 28, y: 80 + resultIndex * 28 },
        width: size.width,
        height: size.height,
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
