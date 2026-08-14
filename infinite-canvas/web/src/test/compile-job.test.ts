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
});
