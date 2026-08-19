import { describe, expect, it } from "vitest";

import type { ModelSpec } from "@/api/contracts";
import { ADMIN_MODEL_TEMPLATES } from "@/components/admin/model-templates";
import { compileGraphJob, CompileJobError } from "@/features/graph/compile-job";
import { GRAPH_SCHEMA_VERSION } from "@/features/graph/contracts";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from "@/types/canvas";

const model: ModelSpec = {
    model_id: "seedream",
    service_id: "ark-image",
    display_name: "Seedream",
    operations: ["image.generate", "image.edit"],
    input_media: ["text", "image"],
    input_ports: [
        { port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 },
        { port_id: "reference_images", media_type: "image", min_items: 0, max_items: 2 },
    ],
    parameter_schema: { type: "object", properties: { label: { type: "string" }, count: { type: "integer" }, enabled: { type: "boolean" } }, additionalProperties: false },
    parameter_mappings: { label: "quality", count: "n", enabled: "watermark" },
};

const nodes: CanvasNodeData[] = [
    { id: "prompt", type: CanvasNodeType.Text, title: "Prompt", position: { x: 0, y: 0 }, width: 1, height: 1, metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "make it green", outputPortId: "prompt" } } },
    {
        id: "images",
        type: CanvasNodeType.Image,
        title: "Images",
        position: { x: 0, y: 0 },
        width: 1,
        height: 1,
        metadata: {
            graph: {
                schemaVersion: GRAPH_SCHEMA_VERSION,
                role: "media-collection",
                mediaType: "image",
                outputPortId: "media",
                items: [
                    { id: "b", assetId: "asset-b", displayName: "b.png", mimeType: "image/png", bytes: 2 },
                    { id: "a", assetId: "asset-a", displayName: "a.png", mimeType: "image/png", bytes: 1 },
                ],
            },
        },
    },
    {
        id: "model",
        type: CanvasNodeType.Config,
        title: "Model",
        position: { x: 0, y: 0 },
        width: 1,
        height: 1,
        metadata: {
            graph: {
                schemaVersion: GRAPH_SCHEMA_VERSION,
                role: "model",
                modelId: "seedream",
                operation: "image.generate",
                inputPorts: [
                    { id: "prompt", accepts: "prompt" },
                    { id: "reference_images", accepts: "image" },
                ],
                outputPortId: "result",
                parameters: { label: "", count: 0, enabled: false },
            },
        },
    },
];
const connections: CanvasConnection[] = [
    { id: "p", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
    { id: "i", fromNodeId: "images", fromPortId: "media", toNodeId: "model", toPortId: "reference_images" },
];

describe("compileGraphJob", () => {
    it("freezes prompt, ordered typed inputs and exact falsy parameters", () => {
        const result = compileGraphJob(nodes, connections, "model", model);
        expect(result).toEqual({ operation: "image.edit", model_id: "seedream", prompt: "make it green", params: { label: "", count: 0, enabled: false }, inputs: { reference_images: ["asset-b", "asset-a"] }, asset_ids: [] });
        expect(Object.isFrozen(result)).toBe(true);
        expect(Object.isFrozen(result.inputs.reference_images)).toBe(true);
    });

    it("accepts a public model catalog that intentionally omits provider mappings", () => {
        const publicModel = { ...model, parameter_mappings: {} };
        expect(compileGraphJob(nodes, connections, "model", publicModel).params).toEqual({ label: "", count: 0, enabled: false });
    });

    it("blocks missing prompt, unknown parameters and exact input limit violations", () => {
        expect(() => compileGraphJob(nodes, connections.slice(1), "model", model)).toThrowError(CompileJobError);
        const tooMany = structuredClone(nodes);
        const graph = tooMany[1].metadata?.graph;
        if (graph?.role !== "media-collection") throw new Error("fixture");
        graph.items.push({ id: "c", assetId: "asset-c", displayName: "c.png", mimeType: "image/png", bytes: 1 });
        expect(() => compileGraphJob(tooMany, connections, "model", model)).toThrow("最多允许 2");
        const unknown = structuredClone(nodes);
        const modelGraph = unknown[2].metadata?.graph;
        if (modelGraph?.role !== "model") throw new Error("fixture");
        modelGraph.parameters.unknown = 1;
        expect(() => compileGraphJob(unknown, connections, "model", model)).toThrow("不支持的参数");
    });

    it("accepts the size tier preset and ratio enum values including a custom size", () => {
        const ratioModel: ModelSpec = {
            model_id: "seedream-pro",
            service_id: "ark-image",
            display_name: "Seedream Pro",
            operations: ["image.generate"],
            input_media: ["text"],
            input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }],
            parameter_schema: {
                type: "object",
                properties: {
                    size: { type: "string", default: "2K", "x-ark-size": { presets: ["1K", "1.5K", "2K"], min_pixels: 921600, max_pixels: 4624220, min_ratio: 0.0625, max_ratio: 16 } },
                    ratio: { type: "string", enum: ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9"], default: "1:1" },
                },
                additionalProperties: false,
            },
            parameter_mappings: { size: "size", ratio: "ratio" },
        };
        const ratioNodes: CanvasNodeData[] = [
            { id: "prompt", type: CanvasNodeType.Text, title: "Prompt", position: { x: 0, y: 0 }, width: 1, height: 1, metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "make it", outputPortId: "prompt" } } },
            {
                id: "model",
                type: CanvasNodeType.Config,
                title: "Model",
                position: { x: 0, y: 0 },
                width: 1,
                height: 1,
                metadata: {
                    graph: {
                        schemaVersion: GRAPH_SCHEMA_VERSION,
                        role: "model",
                        modelId: "seedream-pro",
                        operation: "image.generate",
                        inputPorts: [{ id: "prompt", accepts: "prompt" }],
                        outputPortId: "result",
                        parameters: { size: "1.5K", ratio: "16:9" },
                    },
                },
            },
        ];
        const ratioConnections: CanvasConnection[] = [{ id: "p", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" }];
        expect(compileGraphJob(ratioNodes, ratioConnections, "model", ratioModel).params).toEqual({ size: "1.5K", ratio: "16:9" });

        const custom = structuredClone(ratioNodes);
        const customGraph = custom[1].metadata?.graph;
        if (customGraph?.role !== "model") throw new Error("fixture");
        customGraph.parameters = { size: "2048x1024", ratio: "9:16" };
        expect(compileGraphJob(custom, ratioConnections, "model", ratioModel).params).toEqual({ size: "2048x1024", ratio: "9:16" });
    });

    it("enforces the trusted Seedream edit template minimum of one reference image", () => {
        const template = ADMIN_MODEL_TEMPLATES.find((item) => item.id === "seedream")!;
        const seedreamEdit: ModelSpec = {
            model_id: "seedream",
            service_id: "ark",
            display_name: "Seedream",
            operations: ["image.edit"],
            input_media: ["text", "image"],
            input_ports: template.contract.input_ports,
            parameter_schema: template.contract.parameter_schema,
            parameter_mappings: template.contract.parameter_mappings,
        };
        const editNodes = structuredClone(nodes);
        const graph = editNodes[2].metadata?.graph;
        if (graph?.role !== "model") throw new Error("fixture");
        graph.operation = "image.edit";
        graph.parameters = { size: "1K" };

        expect(() => compileGraphJob(editNodes, connections.slice(0, 1), "model", seedreamEdit)).toThrow("reference_images 至少需要 1 个输入");
        expect(compileGraphJob(editNodes, connections, "model", seedreamEdit).inputs.reference_images).toEqual(["asset-b", "asset-a"]);
    });

    it("compiles a protected result output back into an ordered image input", () => {
        const resultNode: CanvasNodeData = {
            id: "result",
            type: CanvasNodeType.Image,
            title: "Result",
            position: { x: 0, y: 0 },
            width: 1,
            height: 1,
            metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "result", mediaType: "image", inputPortId: "result", outputPortId: "media", assetId: "job-result.source.1" } },
        };
        const result = compileGraphJob([...nodes.slice(0, 1), nodes[2], resultNode], [connections[0], { id: "r", fromNodeId: "result", fromPortId: "media", toNodeId: "model", toPortId: "reference_images" }], "model", model);
        expect(result.inputs.reference_images).toEqual(["job-result.source.1"]);
    });

    it("derives the asset id of a legacy result node from its source job", () => {
        const legacyResultNode: CanvasNodeData = {
            id: "legacy-result",
            type: CanvasNodeType.Image,
            title: "生成图片",
            position: { x: 0, y: 0 },
            width: 1,
            height: 1,
            metadata: {
                content: "/api/v1/results/job-legacy",
                status: "success",
                sourceJobId: "job-legacy",
                graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "result", mediaType: "image", inputPortId: "result", outputPortId: "media" },
            },
        };
        const result = compileGraphJob([nodes[0], nodes[2], legacyResultNode], [connections[0], { id: "r", fromNodeId: "legacy-result", fromPortId: "media", toNodeId: "model", toPortId: "reference_images" }], "model", model);
        expect(result.inputs.reference_images).toEqual(["job-result.job-legacy.0"]);
    });

    it("rejects a result reference without any resolvable asset", () => {
        const orphanResultNode: CanvasNodeData = {
            id: "orphan-result",
            type: CanvasNodeType.Image,
            title: "生成图片",
            position: { x: 0, y: 0 },
            width: 1,
            height: 1,
            metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "result", mediaType: "image", inputPortId: "result", outputPortId: "media" } },
        };
        expect(() => compileGraphJob([nodes[0], nodes[2], orphanResultNode], [connections[0], { id: "r", fromNodeId: "orphan-result", fromPortId: "media", toNodeId: "model", toPortId: "reference_images" }], "model", model)).toThrow("reference_images 的连接类型不正确。");
    });
});

describe("compileGraphJob with library assets", () => {
    it("passes library-kind media item asset ids through unchanged", () => {
        const libraryNode: CanvasNodeData = {
            ...nodes[1],
            metadata: {
                graph: {
                    schemaVersion: GRAPH_SCHEMA_VERSION,
                    role: "media-collection",
                    mediaType: "image",
                    outputPortId: "media",
                    items: [{ id: "lib", assetId: "lib-1", displayName: "lib.png", mimeType: "image/png", bytes: 3, kind: "library" }],
                },
            },
        };
        const result = compileGraphJob([nodes[0], libraryNode, nodes[2]], [connections[0], connections[1]], "model", model);
        expect(result.inputs.reference_images).toEqual(["lib-1"]);
    });
});
