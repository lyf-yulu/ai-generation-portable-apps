import { describe, expect, it } from "vitest";

import {
    GRAPH_SCHEMA_VERSION,
    createGraphSubmissionSnapshot,
    type GraphMediaCollectionMetadata,
    type GraphModelMetadata,
    type GraphPromptMetadata,
    type GraphResultMetadata,
} from "@/features/graph/contracts";
import { normalizeCanvasProject, UnsupportedGraphSchemaError, type CanvasProjectInput } from "@/features/graph/normalize-project";
import { createNodeRegistry } from "@/features/nodes/registry";
import { normalizeConnection } from "@/lib/canvas/canvas-node-geometry";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from "@/types/canvas";

const timestamp = "2026-08-11T01:02:03.000Z";

// Canonical edges are never allowed to lose their port identity after deserialization.
// @ts-expect-error legacy port-less edges belong at the normalization input boundary
const portlessCanonicalConnection: CanvasConnection = { id: "legacy", fromNodeId: "a", toNodeId: "b" };
void portlessCanonicalConnection;

function node(id: string, type: CanvasNodeData["type"], metadata: CanvasNodeData["metadata"] = {}): CanvasNodeData {
    return { id, type, title: id, position: { x: 0, y: 0 }, width: 240, height: 160, metadata };
}

function project(nodes: CanvasNodeData[], connections: CanvasProjectInput["connections"] = []): CanvasProjectInput {
    return {
        id: "project-1",
        title: "Legacy",
        createdAt: timestamp,
        updatedAt: timestamp,
        nodes,
        connections,
        chatSessions: [],
        activeChatId: null,
        backgroundMode: "lines",
        showImageInfo: false,
        viewport: { x: 0, y: 0, k: 1 },
    };
}

describe("graph contracts", () => {
    it("represents the four graph roles with bounded media, ports, and model parameters", () => {
        const prompt: GraphPromptMetadata = { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "镜头向前", outputPortId: "prompt" };
        const collection: GraphMediaCollectionMetadata = {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "media-collection",
            mediaType: "image",
            outputPortId: "media",
            items: [{ id: "item-1", assetId: "asset-1", displayName: "参考图.png", mimeType: "image/png", bytes: 42, width: 64, height: 32 }],
        };
        const model: GraphModelMetadata = {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "model",
            modelId: "seedream-test",
            operation: "image.edit",
            inputPorts: [
                { id: "prompt", accepts: "prompt" },
                { id: "reference_images", accepts: "image" },
            ],
            outputPortId: "result",
            parameters: { count: 2, watermark: false, ratio: "16:9" },
        };
        const result: GraphResultMetadata = {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "result",
            mediaType: "image",
            inputPortId: "result",
            outputPortId: "media",
            assetId: "result-asset",
            jobId: "job-1",
        };

        expect([prompt.role, collection.role, model.role, result.role]).toEqual(["prompt", "media-collection", "model", "result"]);
        expect(collection.items[0]).toMatchObject({ assetId: "asset-1", mimeType: "image/png", bytes: 42 });
        expect(model.parameters).toEqual({ count: 2, watermark: false, ratio: "16:9" });
        expect(model.inputPorts).toEqual([{ id: "prompt", accepts: "prompt" }, { id: "reference_images", accepts: "image" }]);
    });

    it("creates an immutable submission snapshot independent from mutable editor values", () => {
        const source = {
            prompt: "@图片1 向前移动",
            modelId: "seedance-test",
            operation: "video.generate",
            parameters: { duration: 5, generateAudio: true },
            inputs: [{ portId: "reference_images", mediaType: "image" as const, assetIds: ["asset-1", "asset-2"] }],
        };

        const snapshot = createGraphSubmissionSnapshot(source);
        source.prompt = "changed";
        source.parameters.duration = 10;
        source.inputs[0].assetIds.reverse();

        expect(snapshot).toEqual({
            schemaVersion: GRAPH_SCHEMA_VERSION,
            prompt: "@图片1 向前移动",
            modelId: "seedance-test",
            operation: "video.generate",
            parameters: { duration: 5, generateAudio: true },
            inputs: [{ portId: "reference_images", mediaType: "image", assetIds: ["asset-1", "asset-2"] }],
        });
        expect(Object.isFrozen(snapshot)).toBe(true);
        expect(Object.isFrozen(snapshot.parameters)).toBe(true);
        expect(Object.isFrozen(snapshot.inputs[0].assetIds)).toBe(true);
    });

    it("adds source and target port IDs when interactive legacy nodes are connected", () => {
        const nodes = [node("prompt", CanvasNodeType.Text), node("model", CanvasNodeType.Config), node("result", CanvasNodeType.Video)];

        expect(normalizeConnection("prompt", "model", nodes, "source")).toEqual({
            fromNodeId: "prompt",
            fromPortId: "prompt",
            toNodeId: "model",
            toPortId: "prompt",
        });
        expect(normalizeConnection("model", "result", nodes, "source")).toEqual({
            fromNodeId: "model",
            fromPortId: "result",
            toNodeId: "result",
            toPortId: "result",
        });
    });
});

describe("legacy graph normalization", () => {
    it("migrates built-in nodes and unambiguous named-port connections without changing identity or timestamps", () => {
        const legacy = project(
            [
                node("prompt-a", CanvasNodeType.Text, { content: "第一条" }),
                node("prompt-b", CanvasNodeType.Text, { prompt: "第二条" }),
                node("image", CanvasNodeType.Image, { storageKey: "owned/image.png", mimeType: "image/png", bytes: 99 }),
                node("video", CanvasNodeType.Video, { storageKey: "owned/video.mp4", mimeType: "video/mp4" }),
                node("model", CanvasNodeType.Config, { model: "seedance-test", params: { ratio: "16:9", duration: 5, nested: { unsafe: true } } }),
                node("output", CanvasNodeType.Video, { sourceJobId: "job-1", storageKey: "/api/v1/jobs/job-1/result" }),
            ],
            [
                { id: "prompt-edge", fromNodeId: "prompt-a", toNodeId: "model" },
                { id: "second-prompt", fromNodeId: "prompt-b", toNodeId: "model" },
                { id: "image-edge", fromNodeId: "image", toNodeId: "model" },
                { id: "video-edge", fromNodeId: "video", toNodeId: "model" },
                { id: "output-edge", fromNodeId: "model", toNodeId: "output" },
            ],
        );

        const normalized = normalizeCanvasProject(legacy);

        expect(normalized).not.toBe(legacy);
        expect(normalized).toMatchObject({ id: "project-1", createdAt: timestamp, updatedAt: timestamp, graphSchemaVersion: GRAPH_SCHEMA_VERSION });
        expect(normalized.nodes.map((item) => item.metadata?.graph?.role)).toEqual(["prompt", "prompt", "result", "result", "model", "result"]);
        expect(normalized.nodes[0].metadata?.graph).toMatchObject({ role: "prompt", text: "第一条", outputPortId: "prompt" });
        expect(normalized.nodes[4].metadata?.graph).toMatchObject({
            role: "model",
            modelId: "seedance-test",
            inputPorts: [
                { id: "prompt", accepts: "prompt" },
                { id: "reference_images", accepts: "image" },
                { id: "first_frame", accepts: "image" },
                { id: "last_frame", accepts: "image" },
                { id: "reference_video", accepts: "video" },
                { id: "reference_audio", accepts: "audio" },
            ],
            parameters: { ratio: "16:9", duration: 5 },
        });
        expect(normalized.connections).toEqual([
            { id: "prompt-edge", fromNodeId: "prompt-a", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "second-prompt", fromNodeId: "prompt-b", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "image-edge", fromNodeId: "image", fromPortId: "media", toNodeId: "model", toPortId: "reference_images" },
            { id: "video-edge", fromNodeId: "video", fromPortId: "media", toNodeId: "model", toPortId: "reference_video" },
            { id: "output-edge", fromNodeId: "model", fromPortId: "result", toNodeId: "output", toPortId: "result" },
        ]);
    });

    it("rejects dangling, self and ambiguous edges while preserving raw duplicates and prompt conflicts", () => {
        const legacy = project(
            [node("prompt-a", CanvasNodeType.Text), node("prompt-b", CanvasNodeType.Text), node("model", CanvasNodeType.Config), node("image-a", CanvasNodeType.Image), node("image-b", CanvasNodeType.Image)],
            [
                { id: "keep-prompt", fromNodeId: "prompt-a", toNodeId: "model" },
                { id: "drop-second-prompt", fromNodeId: "prompt-b", toNodeId: "model" },
                { id: "keep-image", fromNodeId: "image-a", toNodeId: "model" },
                { id: "drop-duplicate", fromNodeId: "image-a", toNodeId: "model" },
                { id: "drop-self", fromNodeId: "model", toNodeId: "model" },
                { id: "drop-dangling", fromNodeId: "missing", toNodeId: "model" },
                { id: "drop-ambiguous", fromNodeId: "image-a", toNodeId: "image-b" },
            ],
        );

        const once = normalizeCanvasProject(legacy);
        const twice = normalizeCanvasProject(once);

        expect(once.connections.map((edge) => edge.id)).toEqual(["keep-prompt", "drop-second-prompt", "keep-image", "drop-duplicate"]);
        expect(twice).toEqual(once);
    });

    it("preserves unknown plugin nodes and already-valid named ports", () => {
        const plugin = node("plugin", "example:processor", { content: "opaque" });
        const secondPlugin = node("plugin-2", "example:sink", { content: "opaque-2" });
        const prompt = node("prompt", CanvasNodeType.Text, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "hello", outputPortId: "prompt" } });
        const model = node("model", CanvasNodeType.Config, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: "model", operation: "custom", inputPorts: [{ id: "custom_input", accepts: "any" }, { id: "prompt", accepts: "prompt" }], outputPortId: "result", parameters: {} } });
        const source = project([plugin, secondPlugin, prompt, model], [
            { id: "plugin-plugin", fromNodeId: "plugin", fromPortId: "custom_output", toNodeId: "plugin-2", toPortId: "custom_input" },
            { id: "plugin-model", fromNodeId: "plugin", fromPortId: "custom_output", toNodeId: "model", toPortId: "custom_input" },
            { id: "plugin-reserved", fromNodeId: "plugin", fromPortId: "custom_output", toNodeId: "model", toPortId: "prompt" },
            { id: "builtin-plugin", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "plugin-2", toPortId: "custom_input" },
        ]);

        const normalized = normalizeCanvasProject(source);

        expect(normalized.nodes.find((item) => item.id === "plugin")).toEqual(plugin);
        expect(normalized.connections).toEqual([
            { id: "plugin-plugin", fromNodeId: "plugin", fromPortId: "custom_output", toNodeId: "plugin-2", toPortId: "custom_input" },
            { id: "plugin-model", fromNodeId: "plugin", fromPortId: "custom_output", toNodeId: "model", toPortId: "custom_input" },
            { id: "plugin-reserved", fromNodeId: "plugin", fromPortId: "custom_output", toNodeId: "model", toPortId: "prompt" },
            { id: "builtin-plugin", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "plugin-2", toPortId: "custom_input" },
        ]);
    });

    it("validates explicit built-in roles and ports before consuming a model prompt slot", () => {
        const prompt = node("prompt", CanvasNodeType.Text, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "hello", outputPortId: "prompt" } });
        const image = node("image", CanvasNodeType.Image, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "result", mediaType: "image", inputPortId: "result", outputPortId: "media" } });
        const model = node("model", CanvasNodeType.Config, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: "model", operation: "image.edit", inputPorts: [{ id: "prompt", accepts: "prompt" }, { id: "reference_images", accepts: "image" }], outputPortId: "result", parameters: {} } });
        const result = node("result", CanvasNodeType.Image, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "result", mediaType: "image", inputPortId: "result", outputPortId: "media" } });
        const source = project([prompt, image, model, result], [
            { id: "invalid-prompt-role", fromNodeId: "image", fromPortId: "media", toNodeId: "model", toPortId: "prompt" },
            { id: "valid-prompt", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "invalid-source-port", fromNodeId: "prompt", fromPortId: "media", toNodeId: "model", toPortId: "prompt" },
            { id: "invalid-model-input", fromNodeId: "image", fromPortId: "media", toNodeId: "model", toPortId: "first_frame" },
            { id: "partial-explicit-port", fromNodeId: "image", fromPortId: "media", toNodeId: "model" },
            { id: "valid-image", fromNodeId: "image", fromPortId: "media", toNodeId: "model", toPortId: "reference_images" },
            { id: "invalid-model-output", fromNodeId: "model", fromPortId: "media", toNodeId: "result", toPortId: "result" },
        ]);

        expect(normalizeCanvasProject(source).connections.map((edge) => edge.id)).toEqual(["valid-prompt", "valid-image"]);
    });

    it("keeps only canonical model-result built-in edges valid across reload while preserving plugin raw data", () => {
        const plugin = node("plugin", "plugin.result");
        const prompt = node("prompt", CanvasNodeType.Text, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "x", outputPortId: "prompt" } });
        const image = node("image", CanvasNodeType.Image, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "result", mediaType: "image", inputPortId: "result", outputPortId: "media" } });
        const model = node("model", CanvasNodeType.Config, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: "model", operation: "image.generate", inputPorts: [], outputPortId: "result", parameters: {} } });
        const result = node("result", CanvasNodeType.Image, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "result", mediaType: "image", inputPortId: "result", outputPortId: "media" } });
        const source = project([plugin, prompt, image, model, result], [
            { id: "plugin-spoof", fromNodeId: "plugin", fromPortId: "out", toNodeId: "result", toPortId: "result" },
            { id: "prompt-spoof", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "result", toPortId: "result" },
            { id: "media-spoof", fromNodeId: "image", fromPortId: "media", toNodeId: "result", toPortId: "result" },
            { id: "model-result", fromNodeId: "model", fromPortId: "result", toNodeId: "result", toPortId: "result" },
        ]);

        const once = normalizeCanvasProject(source);
        expect(once.connections.map((edge) => edge.id)).toEqual(["plugin-spoof", "model-result"]);
        expect(normalizeCanvasProject(once)).toEqual(once);
    });

    it("migrates legacy model inputPortIds to typed descriptors and deep-clones canonical descriptors", () => {
        const legacyModel = node("legacy-model", CanvasNodeType.Config, {
            graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: "legacy", operation: "custom", inputPortIds: ["prompt", "custom_input"], outputPortId: "result", parameters: {} } as never,
        });
        const canonicalModel = node("canonical-model", CanvasNodeType.Config, {
            graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: "canonical", operation: "custom", inputPorts: [{ id: "reference_audio", accepts: "audio" }, { id: "custom_input", accepts: "any" }], outputPortId: "result", parameters: {} },
        });
        const source = project([legacyModel, canonicalModel]);

        const normalized = normalizeCanvasProject(source);
        const canonicalSource = canonicalModel.metadata!.graph!;
        if (canonicalSource.role !== "model") throw new Error("expected model metadata");
        canonicalSource.inputPorts[0].accepts = "image";

        expect(normalized.nodes[0].metadata?.graph).toMatchObject({
            role: "model",
            inputPorts: [{ id: "prompt", accepts: "prompt" }, { id: "custom_input", accepts: "any" }],
        });
        expect(normalized.nodes[1].metadata?.graph).toMatchObject({
            role: "model",
            inputPorts: [{ id: "reference_audio", accepts: "audio" }, { id: "custom_input", accepts: "any" }],
        });
        expect(normalized.nodes[0].metadata?.graph).not.toHaveProperty("inputPortIds");
    });

    it.each([
        ["too many", Array.from({ length: 33 }, (_, index) => `input_${index}`)],
        ["duplicate", ["prompt", "prompt"]],
        ["empty", [""]],
        ["space", ["unsafe port"]],
        ["control", ["unsafe\nport"]],
        ["unicode", ["参考图"]],
        ["long", ["a".repeat(65)]],
    ] as const)("rejects unsafe legacy model inputPortIds without mutating input: %s", (_name, inputPortIds) => {
        const legacy = node("legacy", CanvasNodeType.Config, { graph: {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "model",
            modelId: "legacy",
            operation: "custom",
            inputPortIds,
            outputPortId: "result",
            parameters: {},
        } as never });
        const source = project([legacy]);
        const before = JSON.stringify(source);

        expect(() => normalizeCanvasProject(source)).toThrow("Invalid graph port declaration");
        expect(JSON.stringify(source)).toBe(before);
    });

    it.each(["", "unsafe output", "unsafe\noutput", "结果", "a".repeat(65)])("rejects unsafe legacy model outputPortId %j", (outputPortId) => {
        const legacy = node("legacy", CanvasNodeType.Config, { graph: {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "model",
            modelId: "legacy",
            operation: "custom",
            inputPortIds: ["prompt"],
            outputPortId,
            parameters: {},
        } as never });
        expect(() => normalizeCanvasProject(project([legacy]))).toThrow("Invalid graph port declaration");
    });

    it.each([
        [CanvasNodeType.Text, { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "x", outputPortId: "bad port" }],
        [CanvasNodeType.Image, { schemaVersion: GRAPH_SCHEMA_VERSION, role: "media-collection", mediaType: "image", outputPortId: "媒体", items: [] }],
        [CanvasNodeType.Video, { schemaVersion: GRAPH_SCHEMA_VERSION, role: "result", mediaType: "video", outputPortId: "bad\nport" }],
    ] as const)("validates legacy %s output IDs through the shared graph boundary", (type, graph) => {
        expect(() => normalizeCanvasProject(project([node("legacy", type, { graph: graph as never })]))).toThrow("Invalid graph port declaration");
    });

    it("migrates pre-target result metadata without losing persisted result ownership", () => {
        const legacyResult = node("result", CanvasNodeType.Video, { graph: {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "result",
            mediaType: "video",
            outputPortId: "media",
            assetId: "asset-owned",
            jobId: "job-owned",
        } as never });

        expect(normalizeCanvasProject(project([legacyResult])).nodes[0].metadata?.graph).toEqual({
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "result",
            mediaType: "video",
            inputPortId: "result",
            outputPortId: "media",
            assetId: "asset-owned",
            jobId: "job-owned",
        });
    });

    it("validates and preserves typed plugin edges to every standard model port across reload", () => {
        const registry = createNodeRegistry();
        registry.registerNode({ id: "plugin.prompt", version: 1, title: "Prompt", inputs: [], outputs: [{ id: "out", provides: "prompt" }], createMetadata: () => ({}), render: () => null });
        registry.registerNode({ id: "plugin.image", version: 1, title: "Image", inputs: [], outputs: [{ id: "out", provides: "image" }], createMetadata: () => ({}), render: () => null });
        registry.registerNode({ id: "plugin.video", version: 1, title: "Video", inputs: [], outputs: [{ id: "out", provides: "video" }], createMetadata: () => ({}), render: () => null });
        registry.registerNode({ id: "plugin.audio", version: 1, title: "Audio", inputs: [], outputs: [{ id: "out", provides: "audio" }], createMetadata: () => ({}), render: () => null });
        const model = node("model", CanvasNodeType.Config, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: "model", operation: "video.generate", inputPorts: [
            { id: "prompt", accepts: "prompt" },
            { id: "reference_images", accepts: "image" },
            { id: "first_frame", accepts: "image" },
            { id: "last_frame", accepts: "image" },
            { id: "reference_video", accepts: "video" },
            { id: "reference_audio", accepts: "audio" },
        ], outputPortId: "result", parameters: {} } });
        const nodes = [node("plugin-prompt", "plugin.prompt"), node("plugin-image", "plugin.image"), node("plugin-video", "plugin.video"), node("plugin-audio", "plugin.audio"), model];
        const connections = [
            { id: "prompt", fromNodeId: "plugin-prompt", fromPortId: "out", toNodeId: "model", toPortId: "prompt" },
            { id: "images", fromNodeId: "plugin-image", fromPortId: "out", toNodeId: "model", toPortId: "reference_images" },
            { id: "first", fromNodeId: "plugin-image", fromPortId: "out", toNodeId: "model", toPortId: "first_frame" },
            { id: "last", fromNodeId: "plugin-image", fromPortId: "out", toNodeId: "model", toPortId: "last_frame" },
            { id: "video", fromNodeId: "plugin-video", fromPortId: "out", toNodeId: "model", toPortId: "reference_video" },
            { id: "audio", fromNodeId: "plugin-audio", fromPortId: "out", toNodeId: "model", toPortId: "reference_audio" },
        ];

        const once = normalizeCanvasProject(project(nodes, connections), registry);
        const twice = normalizeCanvasProject(once, registry);

        expect(once.connections.map((edge) => edge.id)).toEqual(["prompt", "images", "first", "last", "video", "audio"]);
        expect(twice).toEqual(once);
    });

    it("preserves plugin-boundary edges independently from registry timing", () => {
        const registry = createNodeRegistry();
        registry.registerNode({ id: "plugin.any", version: 1, title: "Any", inputs: [], outputs: ["out"], createMetadata: () => ({}), render: () => null });
        const unknown = node("unknown", "plugin.unknown");
        const untyped = node("untyped", "plugin.any");
        const prompt = node("prompt", CanvasNodeType.Text, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "valid", outputPortId: "prompt" } });
        const model = node("model", CanvasNodeType.Config, { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: "model", operation: "image.generate", inputPorts: [{ id: "prompt", accepts: "prompt" }, { id: "reference_images", accepts: "image" }], outputPortId: "result", parameters: {} } });
        const source = project([unknown, untyped, prompt, model], [
            { id: "opaque-prompt", fromNodeId: "unknown", fromPortId: "mystery", toNodeId: "model", toPortId: "prompt" },
            { id: "drop-untyped", fromNodeId: "untyped", fromPortId: "out", toNodeId: "model", toPortId: "reference_images" },
            { id: "valid-prompt", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
        ]);

        const normalized = normalizeCanvasProject(source, registry);

        expect(normalized.connections.map((edge) => edge.id)).toEqual(["opaque-prompt", "drop-untyped", "valid-prompt"]);
        expect(normalizeCanvasProject(normalized).connections).toEqual(normalized.connections);

        const typedRegistry = createNodeRegistry();
        typedRegistry.registerNode({ id: "plugin.unknown", version: 1, title: "Unknown", inputs: [], outputs: [{ id: "mystery", provides: "prompt" }], createMetadata: () => ({}), render: () => null });
        typedRegistry.registerNode({ id: "plugin.any", version: 2, title: "Changed", inputs: [], outputs: [{ id: "out", provides: "image" }], createMetadata: () => ({}), render: () => null });
        expect(normalizeCanvasProject(source, typedRegistry).connections).toEqual(normalized.connections);
    });

    it.each([
        ["duplicate", [{ id: "same", accepts: "image" }, { id: "same", accepts: "video" }], "result"],
        ["too many", Array.from({ length: 33 }, (_, index) => ({ id: `input_${index}`, accepts: "image" })), "result"],
        ["long", [{ id: "a".repeat(65), accepts: "image" }], "result"],
        ["unsafe", [{ id: "unsafe port", accepts: "image" }], "result"],
        ["unsafe output", [{ id: "prompt", accepts: "prompt" }], "unsafe output"],
    ] as const)("rejects unsafe canonical graph port declarations: %s", (_name, inputPorts, outputPortId) => {
        const malformed = node("model", CanvasNodeType.Config, { graph: {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "model",
            modelId: "model",
            operation: "image.generate",
            inputPorts,
            outputPortId,
            parameters: {},
        } as never });
        expect(() => normalizeCanvasProject(project([malformed]))).toThrow("Invalid graph port declaration");
    });

    it("rebuilds malformed current-version metadata from bounded legacy fields", () => {
        const malformed = node("image", CanvasNodeType.Image, {
            mimeType: "image/png",
            graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "media-collection", mediaType: "image" } as never,
        });

        const normalized = normalizeCanvasProject(project([malformed]));

        expect(normalized.nodes[0].metadata?.graph).toEqual({
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "result",
            mediaType: "image",
            inputPortId: "result",
            outputPortId: "media",
        });
    });

    it.each([
        [CanvasNodeType.Image, "image"],
        [CanvasNodeType.Video, "video"],
        [CanvasNodeType.Audio, "audio"],
    ] as const)("does not promote legacy %s reference asset IDs into a result asset", (nodeType, mediaType) => {
        const legacy = node(`failed-${mediaType}`, nodeType, {
            status: "error",
            assetIds: ["reference-input-only"],
            sourceJobId: "job-failed",
        });

        const normalized = normalizeCanvasProject(project([legacy]));

        expect(normalized.nodes[0].metadata?.assetIds).toEqual(["reference-input-only"]);
        expect(normalized.nodes[0].metadata?.graph).toEqual({
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "result",
            mediaType,
            inputPortId: "result",
            outputPortId: "media",
            jobId: "job-failed",
        });
    });

    it.each(["1", true, false, 0, -1, 1.5, 2, null])("rejects present non-v1 project graph version %j", (version) => {
        const source = { ...project([]), graphSchemaVersion: version } as never;
        expect(() => normalizeCanvasProject(source)).toThrow(UnsupportedGraphSchemaError);
    });

    it.each(["1", true, false, 0, -1, 1.5, 2, null])("rejects present non-v1 node graph version %j", (version) => {
        const source = node("versioned", CanvasNodeType.Text, {
            graph: { schemaVersion: version, role: "prompt", text: "unsafe", outputPortId: "prompt" } as never,
        });
        expect(() => normalizeCanvasProject(project([source]))).toThrow(UnsupportedGraphSchemaError);
    });

    it("deep clones plugin metadata, legacy arrays, and chat message details without invoking getters", () => {
        let getterCalls = 0;
        const plugin = node("plugin", "example:nested", {
            params: { nested: { value: "original" } },
            references: ["reference-a"],
            assetIds: ["asset-a"],
        });
        const source = project([plugin]);
        source.chatSessions = [{
            id: "chat",
            title: "Chat",
            createdAt: timestamp,
            updatedAt: timestamp,
            messages: [{ id: "message", role: "assistant", text: "answer", detail: { nested: { value: "original" } }, references: [{ id: "ref", type: CanvasNodeType.Text, title: "Reference", text: "original" }] }],
        }];

        const normalized = normalizeCanvasProject(source);
        ((source.nodes[0].metadata?.params as { nested: { value: string } }).nested.value) = "changed";
        source.nodes[0].metadata!.references![0] = "changed";
        source.nodes[0].metadata!.assetIds![0] = "changed";
        ((source.chatSessions[0].messages[0].detail as { nested: { value: string } }).nested.value) = "changed";
        source.chatSessions[0].messages[0].references![0].text = "changed";

        expect(normalized.nodes[0].metadata?.params).toEqual({ nested: { value: "original" } });
        expect(normalized.nodes[0].metadata?.references).toEqual(["reference-a"]);
        expect(normalized.nodes[0].metadata?.assetIds).toEqual(["asset-a"]);
        expect(normalized.chatSessions[0].messages[0].detail).toEqual({ nested: { value: "original" } });
        expect(normalized.chatSessions[0].messages[0].references?.[0].text).toBe("original");

        const accessorProject = project([]) as CanvasProjectInput;
        Object.defineProperty(accessorProject, "graphSchemaVersion", { enumerable: true, get: () => { getterCalls += 1; return GRAPH_SCHEMA_VERSION; } });
        expect(() => normalizeCanvasProject(accessorProject)).toThrow(TypeError);
        expect(getterCalls).toBe(0);
    });

    it("rejects a future graph schema without overwriting its opaque metadata", () => {
        const future = node("future", CanvasNodeType.Text, {
            content: "legacy fallback must not replace this",
            graph: { schemaVersion: GRAPH_SCHEMA_VERSION + 1, role: "future-role", opaque: { value: 42 } } as never,
        });
        const source = project([future]);

        expect(() => normalizeCanvasProject(source)).toThrow(UnsupportedGraphSchemaError);
        expect((source.nodes[0].metadata?.graph as unknown as { opaque: { value: number } }).opaque.value).toBe(42);
    });
});
