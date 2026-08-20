import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GRAPH_SCHEMA_VERSION } from "@/features/graph/contracts";
import { connectGraphPorts, getNodePorts, resolveActiveConnections, type GraphPortRef } from "@/features/graph/connect";
import { normalizeCanvasProject } from "@/features/graph/normalize-project";
import { createComfyWorkflowNode } from "@/features/nodes/comfy-workflow";
import { createNodeRegistry, nodeRegistry } from "@/features/nodes/registry";
import CanvasProjectPage from "@/pages/canvas/project";
import { useCanvasStore, type CanvasProject } from "@/stores/canvas/use-canvas-store";
import { clearStorageScope, setScopedStoreFactoryForTest, setStorageScope } from "@/storage/scope";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from "@/types/canvas";

function baseNode(id: string, type: CanvasNodeData["type"], x: number, y: number, metadata: CanvasNodeData["metadata"]): CanvasNodeData {
    return { id, type, title: id, position: { x, y }, width: 240, height: 160, metadata };
}

function promptNode(id: string, x = 40, y = 50) {
    return baseNode(id, CanvasNodeType.Text, x, y, {
        content: id,
        status: "idle",
        graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: id, outputPortId: "prompt" },
    });
}

function modelNode(id: string, inputPortIds: string[], x = 420, y = 80) {
    const accepts: Record<string, "prompt" | "image" | "video" | "audio" | "any"> = {
        prompt: "prompt",
        reference_images: "image",
        first_frame: "image",
        last_frame: "image",
        reference_video: "video",
        reference_audio: "audio",
    };
    return baseNode(id, CanvasNodeType.Config, x, y, {
        status: "idle",
        graph: {
            schemaVersion: GRAPH_SCHEMA_VERSION,
            role: "model",
            modelId: "declared-model",
            operation: "video.generate",
            inputPorts: inputPortIds.map((portId) => ({ id: portId, accepts: accepts[portId] ?? "any" })),
            outputPortId: "result",
            parameters: {},
        },
    });
}

function mediaNode(id: string, mediaType: "image" | "video" | "audio", x = 40, y = 280) {
    const type = mediaType === "image" ? CanvasNodeType.Image : mediaType === "video" ? CanvasNodeType.Video : CanvasNodeType.Audio;
    return baseNode(id, type, x, y, {
        status: "success",
        content: mediaType === "image" ? "/api/v1/results/image" : undefined,
        graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "result", mediaType, inputPortId: "result", outputPortId: "media", assetId: `${id}-asset` },
    });
}

function port(nodeId: string, portId: string, direction: GraphPortRef["direction"]): GraphPortRef {
    return { nodeId, portId, direction };
}

async function renderProject(nodes: CanvasNodeData[], connections: CanvasConnection[] = [], viewport = { x: 0, y: 0, k: 1 }) {
    await setStorageScope({ environment: "test", userId: "connection-user" });
    const projectId = useCanvasStore.getState().createProject("Connection Canvas");
    useCanvasStore.getState().updateProject(projectId, { nodes, connections, viewport });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ models: [] }), { headers: { "content-type": "application/json" } })));
    const view = render(
        <MemoryRouter initialEntries={[`/canvas/${projectId}`]}>
            <Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes>
        </MemoryRouter>,
    );
    return { projectId, ...view };
}

beforeEach(() => {
    useCanvasStore.setState({
        projects: [],
        projectSyncMetadata: {},
        syncNotice: null,
        loadError: null,
        hydrated: true,
        projectsLoaded: true,
    });
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearStorageScope();
    setScopedStoreFactoryForTest();
});

describe("named-port graph rules", () => {
    it("connects typed prompt data to one static ComfyUI workflow node", () => {
        const prompt = promptNode("prompt");
        const workflow = createComfyWorkflowNode({
            workflowId: "wf-1",
            revision: 2,
            title: "Core",
            inputs: [{ id: "prompt", accepts: "prompt" }],
            executionEnabled: false,
        });
        workflow.id = "workflow";

        expect(connectGraphPorts(port("prompt", "prompt", "source"), port("workflow", "prompt", "target"), [prompt, workflow], [], "prompt-workflow")).toMatchObject({
            ok: true,
            connection: { fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "workflow", toPortId: "prompt" },
        });
    });

    it("keeps a ComfyUI workflow output connected to a result node after normalization", () => {
        const workflow = createComfyWorkflowNode({ workflowId: "wf-1", revision: 2, title: "Core", inputs: [], executionEnabled: false });
        workflow.id = "workflow";
        const result = mediaNode("result", "image");
        const connection = connectGraphPorts(port("workflow", "result", "source"), port("result", "result", "target"), [workflow, result], [], "workflow-result");

        expect(connection).toMatchObject({ ok: true });
        if (!connection.ok) throw new Error("expected a ComfyUI result connection");
        const normalized = normalizeCanvasProject({
            id: "project", title: "Project", createdAt: "2026-08-16T00:00:00.000Z", updatedAt: "2026-08-16T00:00:00.000Z",
            nodes: [workflow, result], connections: [connection.connection], chatSessions: [], activeChatId: null,
            backgroundMode: "lines", showImageInfo: false, viewport: { x: 0, y: 0, k: 1 },
        });

        expect(normalized.connections).toEqual([connection.connection]);
        expect(resolveActiveConnections(normalized.connections, normalized.nodes).map(({ active }) => active)).toEqual([true]);
    });

    it("exposes only stable ports declared by graph metadata", () => {
        const model = modelNode("model", ["prompt", "first_frame", "reference_audio"]);

        expect(getNodePorts(model)).toEqual({
            sources: [{ nodeId: "model", portId: "result", direction: "source", valueType: "result", label: "结果" }],
            targets: [
                { nodeId: "model", portId: "prompt", direction: "target", valueType: "prompt", label: "提示词" },
                { nodeId: "model", portId: "first_frame", direction: "target", valueType: "image", label: "首帧" },
                { nodeId: "model", portId: "reference_audio", direction: "target", valueType: "audio", label: "参考音频" },
            ],
        });
    });

    it("exposes result nodes as a model result target and a reusable media source", () => {
        const result = mediaNode("result", "video");
        const model = modelNode("model", []);
        const ports = getNodePorts(result);

        expect(ports).toEqual({
            sources: [{ nodeId: "result", portId: "media", direction: "source", valueType: "video", label: "媒体" }],
            targets: [{ nodeId: "result", portId: "result", direction: "target", valueType: "result", label: "结果" }],
        });
        expect(connectGraphPorts(port("model", "result", "source"), port("result", "result", "target"), [model, result], [], "output")).toMatchObject({ ok: true });
    });

    it("reserves result inputs for declared graph model outputs and rejects typed plugin spoofing", () => {
        const registry = createNodeRegistry();
        registry.registerNode({ id: "plugin.result", version: 1, title: "Spoof", inputs: [], outputs: [{ id: "out", provides: "result" }], createMetadata: () => ({}), render: () => null });
        const plugin = baseNode("plugin", "plugin.result", 0, 0, {});
        const result = mediaNode("result", "image");
        const model = modelNode("model", []);
        const prompt = promptNode("prompt");
        const image = mediaNode("image", "image");
        const nodes = [plugin, result, model, prompt, image];

        expect(connectGraphPorts(port("plugin", "out", "source"), port("result", "result", "target"), nodes, [], "plugin", registry)).toEqual({ ok: false, reason: "incompatible" });
        expect(connectGraphPorts(port("prompt", "prompt", "source"), port("result", "result", "target"), nodes, [], "prompt", registry)).toEqual({ ok: false, reason: "incompatible" });
        expect(connectGraphPorts(port("image", "media", "source"), port("result", "result", "target"), nodes, [], "image", registry)).toEqual({ ok: false, reason: "incompatible" });
        const raw: CanvasConnection[] = [
            { id: "plugin", fromNodeId: "plugin", fromPortId: "out", toNodeId: "result", toPortId: "result" },
            { id: "prompt", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "result", toPortId: "result" },
            { id: "image", fromNodeId: "image", fromPortId: "media", toNodeId: "result", toPortId: "result" },
            { id: "model", fromNodeId: "model", fromPortId: "result", toNodeId: "result", toPortId: "result" },
        ];
        expect(resolveActiveConnections(raw, nodes, registry).map(({ connection, active }) => [connection.id, active])).toEqual([
            ["plugin", false], ["prompt", false], ["image", false], ["model", true],
        ]);
    });

    it("resolves custom plugin ports from an isolated typed registry without title-based rules", () => {
        const registry = createNodeRegistry();
        registry.registerNode({ id: "plugin.source", version: 1, title: "可改名来源", inputs: [], outputs: [{ id: "custom_image", provides: "image" }], createMetadata: () => ({}), render: () => null });
        registry.registerNode({ id: "plugin.pipe", version: 1, title: "可改名处理", inputs: ["anything"], outputs: ["processed"], createMetadata: () => ({}), render: () => null });
        const pluginSource = baseNode("plugin-source", "plugin.source", 0, 0, {});
        const pluginPipe = baseNode("plugin-pipe", "plugin.pipe", 0, 0, {});
        const image = mediaNode("image", "image");
        const model = modelNode("model", ["custom_input"]);
        const modelGraph = model.metadata!.graph!;
        if (modelGraph.role !== "model") throw new Error("expected model metadata");
        modelGraph.inputPorts = [{ id: "custom_input", accepts: "image" }];
        const nodes = [pluginSource, pluginPipe, image, model];

        expect(getNodePorts(pluginSource, registry).sources).toEqual([{ nodeId: "plugin-source", portId: "custom_image", direction: "source", valueType: "image", label: "custom_image" }]);
        expect(connectGraphPorts(port("plugin-source", "custom_image", "source"), port("model", "custom_input", "target"), nodes, [], "plugin-model", registry)).toMatchObject({ ok: true });
        expect(connectGraphPorts(port("image", "media", "source"), port("plugin-pipe", "anything", "target"), nodes, [], "builtin-plugin", registry)).toMatchObject({ ok: true });
        expect(connectGraphPorts(port("plugin-source", "custom_image", "source"), port("plugin-pipe", "anything", "target"), nodes, [], "plugin-plugin", registry)).toMatchObject({ ok: true });
    });

    it("accepts compatible named ports and rejects self, duplicate, incompatible and second-prompt edges", () => {
        const nodes = [promptNode("prompt-a"), promptNode("prompt-b"), mediaNode("image", "image"), modelNode("model", ["prompt", "first_frame", "reference_audio"])];
        const accepted = connectGraphPorts(port("prompt-a", "prompt", "source"), port("model", "prompt", "target"), nodes, [], "edge-prompt");
        expect(accepted).toEqual({
            ok: true,
            connection: { id: "edge-prompt", fromNodeId: "prompt-a", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
        });
        const existing = accepted.ok ? [accepted.connection] : [];

        expect(connectGraphPorts(port("model", "result", "source"), port("model", "prompt", "target"), nodes, existing, "self")).toEqual({ ok: false, reason: "self" });
        expect(connectGraphPorts(port("prompt-a", "prompt", "source"), port("model", "prompt", "target"), nodes, existing, "duplicate")).toEqual({ ok: false, reason: "duplicate" });
        expect(connectGraphPorts(port("image", "media", "source"), port("model", "reference_audio", "target"), nodes, existing, "wrong-media")).toEqual({ ok: false, reason: "incompatible" });
        expect(connectGraphPorts(port("prompt-b", "prompt", "source"), port("model", "prompt", "target"), nodes, existing, "second-prompt")).toEqual({ ok: false, reason: "prompt-occupied" });
        expect(connectGraphPorts(port("image", "media", "source"), port("model", "first_frame", "target"), nodes, existing, "first-frame")).toMatchObject({ ok: true });
    });

    it("keeps reserved model ports strict even when persisted descriptors claim any", () => {
        const prompt = promptNode("prompt");
        const image = mediaNode("image", "image");
        const model = modelNode("model", ["prompt", "reference_audio"]);
        const graph = model.metadata!.graph!;
        if (graph.role !== "model") throw new Error("expected model metadata");
        graph.inputPorts = [{ id: "prompt", accepts: "any" }, { id: "reference_audio", accepts: "any" }];
        const nodes = [prompt, image, model];

        expect(connectGraphPorts(port("image", "media", "source"), port("model", "prompt", "target"), nodes, [], "image-prompt")).toEqual({ ok: false, reason: "incompatible" });
        expect(connectGraphPorts(port("image", "media", "source"), port("model", "reference_audio", "target"), nodes, [], "image-audio")).toEqual({ ok: false, reason: "incompatible" });
        expect(connectGraphPorts(port("prompt", "prompt", "source"), port("model", "prompt", "target"), nodes, [], "prompt-model")).toMatchObject({ ok: true });
    });

    it("connects trusted typed plugin outputs to every matching standard model port", () => {
        const registry = createNodeRegistry();
        registry.registerNode({ id: "plugin.prompt", version: 1, title: "Prompt Provider", inputs: [], outputs: [{ id: "out", provides: "prompt" }], createMetadata: () => ({}), render: () => null });
        registry.registerNode({ id: "plugin.image", version: 1, title: "Image Provider", inputs: [], outputs: [{ id: "out", provides: "image" }], createMetadata: () => ({}), render: () => null });
        registry.registerNode({ id: "plugin.video", version: 1, title: "Video Provider", inputs: [], outputs: [{ id: "out", provides: "video" }], createMetadata: () => ({}), render: () => null });
        registry.registerNode({ id: "plugin.audio", version: 1, title: "Audio Provider", inputs: [], outputs: [{ id: "out", provides: "audio" }], createMetadata: () => ({}), render: () => null });
        const prompt = baseNode("plugin-prompt", "plugin.prompt", 0, 0, {});
        const image = baseNode("plugin-image", "plugin.image", 0, 0, {});
        const video = baseNode("plugin-video", "plugin.video", 0, 0, {});
        const audio = baseNode("plugin-audio", "plugin.audio", 0, 0, {});
        const model = modelNode("model", ["prompt", "reference_images", "first_frame", "last_frame", "reference_video", "reference_audio"]);
        const nodes = [prompt, image, video, audio, model];
        const cases = [
            [prompt, "prompt"],
            [image, "reference_images"],
            [image, "first_frame"],
            [image, "last_frame"],
            [video, "reference_video"],
            [audio, "reference_audio"],
        ] as const;

        for (const [source, targetPortId] of cases) {
            expect(connectGraphPorts(port(source.id, "out", "source"), port("model", targetPortId, "target"), nodes, [], `edge-${targetPortId}`, registry)).toMatchObject({ ok: true });
        }
    });

    it("rejects legacy any outputs and ignores a caller-spoofed valueType for standard ports", () => {
        const registry = createNodeRegistry();
        registry.registerNode({ id: "plugin.legacy", version: 1, title: "Legacy", inputs: [], outputs: ["out"], createMetadata: () => ({}), render: () => null });
        const plugin = baseNode("plugin", "plugin.legacy", 0, 0, {});
        const model = modelNode("model", ["reference_images"]);
        const nodes = [plugin, model];
        const spoofed: GraphPortRef = { nodeId: "plugin", portId: "out", direction: "source", valueType: "image" };

        expect(connectGraphPorts(spoofed, port("model", "reference_images", "target"), nodes, [], "spoofed", registry)).toEqual({ ok: false, reason: "incompatible" });
    });

    it.each(["builtin", "typed-plugin"])("does not let opaque, dangling, any, or spoofed prompt edges block a new %s prompt", (sourceKind) => {
        const registry = createNodeRegistry();
        registry.registerNode({ id: "plugin.prompt", version: 1, title: "Typed Prompt", inputs: [], outputs: [{ id: "out", provides: "prompt" }], createMetadata: () => ({}), render: () => null });
        registry.registerNode({ id: "plugin.any", version: 1, title: "Any", inputs: [], outputs: ["out"], createMetadata: () => ({}), render: () => null });
        const builtinPrompt = promptNode("builtin-prompt");
        const typedPrompt = baseNode("typed-prompt", "plugin.prompt", 0, 0, {});
        const unknown = baseNode("unknown", "plugin.unknown", 0, 0, {});
        const any = baseNode("any", "plugin.any", 0, 0, {});
        const image = mediaNode("image", "image");
        const model = modelNode("model", ["prompt"]);
        const nodes = [builtinPrompt, typedPrompt, unknown, any, image, model];
        const invalidExisting: CanvasConnection[] = [
            { id: "opaque", fromNodeId: "unknown", fromPortId: "mystery", toNodeId: "model", toPortId: "prompt" },
            { id: "dangling", fromNodeId: "missing", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "any", fromNodeId: "any", fromPortId: "out", toNodeId: "model", toPortId: "prompt" },
            { id: "spoof", fromNodeId: "image", fromPortId: "media", toNodeId: "model", toPortId: "prompt" },
            { id: "missing-target-port", fromNodeId: "builtin-prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "removed_prompt" },
        ];
        const source = sourceKind === "builtin" ? port("builtin-prompt", "prompt", "source") : port("typed-prompt", "out", "source");

        const first = connectGraphPorts(source, port("model", "prompt", "target"), nodes, invalidExisting, "valid", registry);
        expect(first).toMatchObject({ ok: true });
        const withValid = first.ok ? [...invalidExisting, first.connection] : invalidExisting;
        const secondSource = sourceKind === "builtin" ? port("typed-prompt", "out", "source") : port("builtin-prompt", "prompt", "source");
        expect(connectGraphPorts(secondSource, port("model", "prompt", "target"), nodes, withValid, "second", registry)).toEqual({ ok: false, reason: "prompt-occupied" });
    });

    it("re-evaluates prompt quota when a plugin registry is missing or changes", () => {
        const typedRegistry = createNodeRegistry();
        typedRegistry.registerNode({ id: "plugin.dynamic", version: 1, title: "Dynamic", inputs: [], outputs: [{ id: "out", provides: "prompt" }], createMetadata: () => ({}), render: () => null });
        const anyRegistry = createNodeRegistry();
        anyRegistry.registerNode({ id: "plugin.dynamic", version: 1, title: "Dynamic", inputs: [], outputs: ["out"], createMetadata: () => ({}), render: () => null });
        const missingRegistry = createNodeRegistry();
        const plugin = baseNode("plugin", "plugin.dynamic", 0, 0, {});
        const builtin = promptNode("builtin");
        const model = modelNode("model", ["prompt"]);
        const nodes = [plugin, builtin, model];
        const existing: CanvasConnection[] = [{ id: "plugin-edge", fromNodeId: "plugin", fromPortId: "out", toNodeId: "model", toPortId: "prompt" }];
        const attempt = (registry: ReturnType<typeof createNodeRegistry>) => connectGraphPorts(port("builtin", "prompt", "source"), port("model", "prompt", "target"), nodes, existing, "builtin-edge", registry);

        expect(attempt(missingRegistry)).toMatchObject({ ok: true });
        expect(attempt(anyRegistry)).toMatchObject({ ok: true });
        expect(attempt(typedRegistry)).toEqual({ ok: false, reason: "prompt-occupied" });
    });

    it("preserves raw prompt edges while resolving at most the first currently valid prompt as active", () => {
        const registry = createNodeRegistry();
        const plugin = baseNode("plugin", "plugin.dynamic", 0, 0, {});
        const builtin = promptNode("builtin");
        const model = modelNode("model", ["prompt"]);
        const nodes = [plugin, builtin, model];
        const connections: CanvasConnection[] = [
            { id: "plugin-edge", fromNodeId: "plugin", fromPortId: "out", toNodeId: "model", toPortId: "prompt" },
            { id: "builtin-edge", fromNodeId: "builtin", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
        ];
        const before = JSON.stringify(connections);

        expect(resolveActiveConnections(connections, nodes, registry).map(({ connection, active, reason }) => [connection.id, active, reason])).toEqual([
            ["plugin-edge", false, "opaque"],
            ["builtin-edge", true, undefined],
        ]);
        registry.registerNode({ id: "plugin.dynamic", version: 1, title: "Dynamic", inputs: [], outputs: [{ id: "out", provides: "prompt" }], createMetadata: () => ({}), render: () => null });
        expect(resolveActiveConnections(connections, nodes, registry).map(({ connection, active, reason }) => [connection.id, active, reason])).toEqual([
            ["plugin-edge", true, undefined],
            ["builtin-edge", false, "prompt-conflict"],
        ]);
        expect(JSON.stringify(connections)).toBe(before);
    });

    it("arbitrates duplicate ids and tuples in raw order with stable transient keys", () => {
        const nodes = [promptNode("prompt-a"), promptNode("prompt-b"), promptNode("prompt-c"), modelNode("model", ["prompt"])];
        const connections: CanvasConnection[] = [
            { id: "same", fromNodeId: "prompt-a", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "same", fromNodeId: "prompt-b", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "tuple-copy", fromNodeId: "prompt-a", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "unique", fromNodeId: "prompt-c", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
        ];

        const resolved = resolveActiveConnections(connections, nodes, createNodeRegistry());
        expect(resolved.map(({ active, reason }) => [active, reason])).toEqual([
            [true, undefined],
            [false, "duplicate-id"],
            [false, "duplicate"],
            [false, "prompt-conflict"],
        ]);
        expect(new Set(resolved.map((state) => state.connectionKey)).size).toBe(4);
        expect(resolved.map((state) => state.connection)).toEqual(connections);
    });
});

describe("canvas named-port interactions", () => {
    it("connects accessible port buttons by click and preserves named ports after a reload-shaped rehydrate", async () => {
        const prompt = promptNode("prompt");
        prompt.title = "故事文本";
        const model = modelNode("model", ["prompt"]);
        model.title = "视频模型";
        const { projectId, unmount } = await renderProject([prompt, model]);
        const source = screen.getByRole("button", { name: "故事文本：提示词输出端口" });
        const target = screen.getByRole("button", { name: "视频模型：提示词输入端口" });
        expect(within(source).getByText("提示词")).toBeVisible();
        expect(within(target).getByText("提示词")).toBeVisible();

        source.focus();
        fireEvent.click(source);
        expect(source).toHaveAttribute("aria-pressed", "true");
        target.focus();
        fireEvent.click(target);

        const saved = useCanvasStore.getState().openProject(projectId)!;
        expect(saved.connections).toEqual([
            expect.objectContaining({ fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" }),
        ]);

        const serialized = JSON.parse(JSON.stringify(saved)) as CanvasProject;
        unmount();
        useCanvasStore.getState().replaceProjects([serialized]);
        render(
            <MemoryRouter initialEntries={[`/canvas/${projectId}`]}>
                <Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes>
            </MemoryRouter>,
        );
        expect(document.querySelectorAll("[data-connection-id]")).toHaveLength(1);
        expect(useCanvasStore.getState().openProject(projectId)?.connections[0]).toMatchObject({ fromPortId: "prompt", toPortId: "prompt" });
    });

    it("connects a media output with a pointer gesture without dragging nodes or swallowing the next keyboard click", async () => {
        const image = mediaNode("image", "image", 60, 260);
        image.title = "图片结果";
        const model = modelNode("model", ["first_frame"], 460, 100);
        model.title = "视频模型";
        const { projectId } = await renderProject([image, model]);
        const source = screen.getByRole("button", { name: "图片结果：媒体输出端口" });
        const target = screen.getByRole("button", { name: "视频模型：首帧输入端口" });

        fireEvent.pointerDown(source, { button: 0, pointerId: 19, clientX: 300, clientY: 340 });
        fireEvent.pointerMove(window, { pointerId: 19, clientX: 460, clientY: 150 });
        fireEvent.pointerUp(target, { button: 0, pointerId: 19, clientX: 460, clientY: 150 });

        const saved = useCanvasStore.getState().openProject(projectId)!;
        expect(saved.connections[0]).toMatchObject({ fromNodeId: "image", fromPortId: "media", toNodeId: "model", toPortId: "first_frame" });
        expect(saved.nodes.map((node) => node.position)).toEqual([{ x: 60, y: 260 }, { x: 460, y: 100 }]);

        useCanvasStore.getState().updateProject(projectId, { connections: [] });
        await new Promise((resolve) => window.setTimeout(resolve, 0));
        fireEvent.click(source);
        fireEvent.click(target);
        expect(useCanvasStore.getState().openProject(projectId)?.connections).toHaveLength(1);
    });

    it("connects model result to a result node by pointer and click with stable result ports", async () => {
        const model = modelNode("model", []);
        const result = mediaNode("result", "video", 760, 80);
        const { projectId } = await renderProject([model, result]);
        const source = screen.getByRole("button", { name: "model：结果输出端口" });
        const target = screen.getByRole("button", { name: "result：结果输入端口" });

        fireEvent.pointerDown(source, { button: 0, pointerId: 51, clientX: 660, clientY: 160 });
        fireEvent.pointerUp(target, { button: 0, pointerId: 51, clientX: 760, clientY: 160 });
        expect(useCanvasStore.getState().openProject(projectId)?.connections[0]).toMatchObject({ fromPortId: "result", toPortId: "result" });

        useCanvasStore.getState().updateProject(projectId, { connections: [] });
        await new Promise((resolve) => window.setTimeout(resolve, 0));
        fireEvent.click(source);
        fireEvent.click(target);
        expect(useCanvasStore.getState().openProject(projectId)?.connections).toHaveLength(1);
    });

    it("renders world-coordinate edge geometry once under pan and zoom", async () => {
        const prompt = promptNode("prompt", 40, 50);
        const model = modelNode("model", ["prompt"], 420, 80);
        const connection: CanvasConnection = { id: "edge", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" };
        await renderProject([prompt, model], [connection], { x: 120, y: -30, k: 2 });

        expect(screen.getByTestId("canvas-world")).toHaveStyle({ transform: "translate(120px, -30px) scale(2)" });
        const path = document.querySelector<SVGPathElement>("[data-connection-id='edge']");
        expect(path).toHaveAttribute("d", "M 280 130 C 350 130, 350 160, 420 160");
    });

    it("updates port anchors from transient measured node height without persisting layout size", async () => {
        const callbacks = new Map<Element, ResizeObserverCallback>();
        const disconnect = vi.fn();
        class TestResizeObserver {
            constructor(private readonly callback: ResizeObserverCallback) {}
            observe(element: Element) { callbacks.set(element, this.callback); }
            unobserve(element: Element) { callbacks.delete(element); }
            disconnect() { disconnect(); }
        }
        vi.stubGlobal("ResizeObserver", TestResizeObserver);
        const edge: CanvasConnection = { id: "measured-edge", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" };
        const { projectId, unmount } = await renderProject([promptNode("prompt"), modelNode("model", ["prompt"])], [edge]);
        const modelElement = screen.getByTestId("draggable-node-model");
        const contentElement = screen.getByTestId("node-content-model");
        const callback = callbacks.get(contentElement);
        expect(callback).toBeDefined();

        act(() => callback?.([{ target: modelElement, contentRect: { width: 240, height: 320 } } as unknown as ResizeObserverEntry], {} as ResizeObserver));

        await waitFor(() => expect(document.querySelector("[data-connection-id='measured-edge']")).toHaveAttribute("d", "M 280 130 C 350 130, 350 240, 420 240"));
        expect(useCanvasStore.getState().openProject(projectId)?.nodes.find((item) => item.id === "model")?.height).toBe(160);
        unmount();
        expect(disconnect).toHaveBeenCalled();
    });

    it("prunes deleted measured sizes and ignores late observer callbacks before recreating an id", async () => {
        const callbacks = new Map<Element, ResizeObserverCallback>();
        class TestResizeObserver {
            constructor(private readonly callback: ResizeObserverCallback) {}
            observe(element: Element) { callbacks.set(element, this.callback); }
            unobserve(element: Element) { callbacks.delete(element); }
            disconnect() {}
        }
        vi.stubGlobal("ResizeObserver", TestResizeObserver);
        const prompt = promptNode("prompt");
        const model = modelNode("model", ["prompt"]);
        const edge: CanvasConnection = { id: "edge", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" };
        const { projectId } = await renderProject([prompt, model], [edge]);
        const modelElement = screen.getByTestId("draggable-node-model");
        const contentElement = screen.getByTestId("node-content-model");
        const lateCallback = callbacks.get(contentElement)!;
        act(() => lateCallback([{ target: contentElement, contentRect: { width: 240, height: 320 } } as unknown as ResizeObserverEntry], {} as ResizeObserver));
        await waitFor(() => expect(document.querySelector("[data-connection-id='edge']")).toHaveAttribute("d", "M 280 130 C 350 130, 350 240, 420 240"));

        fireEvent.keyDown(modelElement, { key: "Enter" });
        fireEvent.keyDown(window, { key: "Delete" });
        await waitFor(() => expect(screen.queryByTestId("draggable-node-model")).not.toBeInTheDocument());
        act(() => lateCallback([{ target: modelElement, contentRect: { width: 240, height: 480 } } as unknown as ResizeObserverEntry], {} as ResizeObserver));

        useCanvasStore.getState().updateProject(projectId, { nodes: [prompt, model], connections: [edge] });
        await waitFor(() => expect(document.querySelector("[data-connection-id='edge']")).toHaveAttribute("d", "M 280 130 C 350 130, 350 160, 420 160"));

        const recreatedElement = screen.getByTestId("draggable-node-model");
        const recreatedContentElement = screen.getByTestId("node-content-model");
        const secondLateCallback = callbacks.get(recreatedContentElement)!;
        act(() => secondLateCallback([{ target: recreatedContentElement, contentRect: { width: 240, height: 360 } } as unknown as ResizeObserverEntry], {} as ResizeObserver));
        fireEvent.keyDown(recreatedElement, { key: "Enter" });
        fireEvent.keyDown(window, { key: "Delete" });
        await waitFor(() => expect(screen.queryByTestId("draggable-node-model")).not.toBeInTheDocument());
        act(() => secondLateCallback([{ target: recreatedElement, contentRect: { width: 240, height: 500 } } as unknown as ResizeObserverEntry], {} as ResizeObserver));
        useCanvasStore.getState().updateProject(projectId, { nodes: [prompt, model], connections: [edge] });
        await waitFor(() => expect(document.querySelector("[data-connection-id='edge']")).toHaveAttribute("d", "M 280 130 C 350 130, 350 160, 420 160"));
    });

    it("selects and deletes an edge with the keyboard, and deletes another from its context menu", async () => {
        const prompt = promptNode("prompt");
        const model = modelNode("model", ["prompt", "first_frame"]);
        const image = mediaNode("image", "image");
        const edges: CanvasConnection[] = [
            { id: "prompt-edge", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "image-edge", fromNodeId: "image", fromPortId: "media", toNodeId: "model", toPortId: "first_frame" },
        ];
        const { projectId } = await renderProject([prompt, model, image], edges);
        const promptEdge = document.querySelector<SVGPathElement>("[data-connection-id='prompt-edge']")!;

        fireEvent.click(promptEdge);
        expect(promptEdge).toHaveAttribute("aria-pressed", "true");
        expect(promptEdge).not.toHaveAttribute("aria-selected");
        fireEvent.keyDown(window, { key: "Delete" });
        expect(useCanvasStore.getState().openProject(projectId)?.connections.map((edge) => edge.id)).toEqual(["image-edge"]);

        const imageEdge = document.querySelector<SVGPathElement>("[data-connection-id='image-edge']")!;
        fireEvent.contextMenu(imageEdge, { clientX: 40, clientY: 70 });
        expect(screen.getByRole("menu", { name: "连接操作" })).toBeVisible();
        fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
        expect(useCanvasStore.getState().openProject(projectId)?.connections).toEqual([]);
    });

    it.each([
        ["self", "model：结果输出端口", "model：提示词输入端口", "不能连接同一个节点。"],
        ["duplicate", "prompt-a：提示词输出端口", "model：提示词输入端口", "这两个端口已经连接。"],
        ["incompatible", "image：媒体输出端口", "model：参考音频输入端口", "这两个端口类型不兼容。"],
        ["prompt-occupied", "prompt-b：提示词输出端口", "model：提示词输入端口", "该模型已有提示词连接，每个模型只允许一个提示词节点。"],
    ])("announces the %s rejection while keeping a valid source available for retry", async (kind, sourceLabel, targetLabel, message) => {
        const promptA = promptNode("prompt-a");
        const promptB = promptNode("prompt-b", 40, 240);
        const image = mediaNode("image", "image", 40, 430);
        const model = modelNode("model", ["prompt", "reference_audio"]);
        const existing: CanvasConnection[] = kind === "duplicate" || kind === "prompt-occupied"
            ? [{ id: "existing", fromNodeId: "prompt-a", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" }]
            : [];
        await renderProject([promptA, promptB, image, model], existing);
        const source = screen.getByRole("button", { name: sourceLabel });

        fireEvent.click(source);
        fireEvent.click(screen.getByRole("button", { name: targetLabel }));

        expect(screen.getByTestId("connection-status")).toHaveTextContent(message);
        expect(source).toHaveAttribute("aria-pressed", "true");
    });

    it("announces an incompatible pointer drop and keeps the source active for another target", async () => {
        await renderProject([mediaNode("image", "image"), modelNode("model", ["reference_audio"])]);
        const source = screen.getByRole("button", { name: "image：媒体输出端口" });
        const target = screen.getByRole("button", { name: "model：参考音频输入端口" });

        fireEvent.pointerDown(source, { button: 0, pointerId: 27, clientX: 100, clientY: 320 });
        fireEvent.pointerUp(target, { button: 0, pointerId: 27, clientX: 420, clientY: 160 });

        expect(screen.getByTestId("connection-status")).toHaveTextContent("这两个端口类型不兼容。");
        expect(source).toHaveAttribute("aria-pressed", "true");

        fireEvent.pointerDown(screen.getByTestId("infinite-canvas"), { button: 0, pointerId: 28 });
        fireEvent.pointerUp(window, { pointerId: 28 });
        expect(screen.queryByTestId("connection-status")).not.toBeInTheDocument();
        expect(source).toHaveAttribute("aria-pressed", "false");
    });

    it("clears a pending source immediately when the source node is deleted", async () => {
        const { projectId } = await renderProject([promptNode("prompt"), modelNode("model", ["prompt"])]);
        const source = screen.getByRole("button", { name: "prompt：提示词输出端口" });
        source.focus();
        fireEvent.click(source);

        fireEvent.keyDown(window, { key: "Delete" });

        await waitFor(() => expect(useCanvasStore.getState().openProject(projectId)?.nodes.map((node) => node.id)).toEqual(["model"]));
        await waitFor(() => expect(screen.getByTestId("connection-status")).toHaveTextContent("连接起点已失效。"));
        expect(document.querySelector("path[stroke-dasharray]")).not.toBeInTheDocument();
    });

    it("clears a pending source when a remote-shaped project replacement removes it", async () => {
        const prompt = promptNode("prompt");
        const model = modelNode("model", ["prompt"]);
        const { projectId } = await renderProject([prompt, model]);
        fireEvent.click(screen.getByRole("button", { name: "prompt：提示词输出端口" }));
        const current = useCanvasStore.getState().openProject(projectId)!;

        useCanvasStore.getState().replaceProjects([{ ...current, nodes: [model], connections: [] }]);

        await waitFor(() => expect(screen.getByTestId("connection-status")).toHaveTextContent("连接起点已失效。"));
        expect(document.querySelector("path[stroke-dasharray]")).not.toBeInTheDocument();
    });

    it("clears a pending model output when that declared port is revoked", async () => {
        const model = modelNode("model", ["prompt"]);
        const { projectId } = await renderProject([model]);
        fireEvent.click(screen.getByRole("button", { name: "model：结果输出端口" }));
        const graph = model.metadata!.graph!;
        if (graph.role !== "model") throw new Error("expected model metadata");

        useCanvasStore.getState().updateProject(projectId, { nodes: [{ ...model, metadata: { ...model.metadata, graph: { ...graph, outputPortId: "replacement_result" } } }] });

        await waitFor(() => expect(screen.getByTestId("connection-status")).toHaveTextContent("连接起点已失效。"));
        expect(screen.queryByRole("button", { name: "model：结果输出端口" })).not.toBeInTheDocument();
        expect(document.querySelector("path[stroke-dasharray]")).not.toBeInTheDocument();
    });

    it.each([
        ["ContextMenu", { key: "ContextMenu" }],
        ["Shift+F10", { key: "F10", shiftKey: true }],
    ])("opens an edge menu with %s and restores focus on Escape", async (_name, shortcut) => {
        const edge: CanvasConnection = { id: "edge", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" };
        await renderProject([promptNode("prompt"), modelNode("model", ["prompt"])], [edge]);
        const trigger = screen.getByRole("button", { name: "连接：prompt 提示词(prompt) 到 model 提示词(prompt)" });
        trigger.focus();

        fireEvent.keyDown(trigger, shortcut);

        const menu = screen.getByRole("menu", { name: "连接操作" });
        expect(menu).toBeVisible();
        const deleteItem = screen.getByRole("menuitem", { name: "删除" });
        await waitFor(() => expect(deleteItem).toHaveFocus());
        fireEvent.keyDown(deleteItem, { key: "Escape" });
        expect(trigger).toHaveFocus();
    });

    it("does not expose an enabled connection gesture in read-only mode", async () => {
        const prompt = promptNode("prompt");
        prompt.title = "只读提示词";
        const model = modelNode("model", ["prompt"]);
        model.title = "只读模型";
        const { projectId } = await renderProject([prompt, model]);
        useCanvasStore.setState({ loadError: { code: "CANVAS_LOAD_FAILED", message: "只读保护", readOnly: true } });

        const source = await screen.findByRole("button", { name: "只读提示词：提示词输出端口" });
        const target = screen.getByRole("button", { name: "只读模型：提示词输入端口" });
        expect(source).toBeDisabled();
        expect(target).toBeDisabled();
        fireEvent.click(source);
        fireEvent.click(target);
        expect(useCanvasStore.getState().openProject(projectId)?.connections).toEqual([]);
    });

    it("renders existing edges as non-focusable presentation in read-only mode", async () => {
        const edge: CanvasConnection = { id: "edge", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" };
        await renderProject([promptNode("prompt"), modelNode("model", ["prompt"])], [edge]);
        useCanvasStore.setState({ loadError: { code: "CANVAS_LOAD_FAILED", message: "只读保护", readOnly: true } });

        await waitFor(() => expect(screen.queryByRole("button", { name: "连接：prompt 提示词(prompt) 到 model 提示词(prompt)" })).not.toBeInTheDocument());
        expect(document.querySelector("[data-connection-id='edge']")).toBeInTheDocument();
    });

    it("subscribes to registry changes, re-resolves active prompts and clears a revoked pending port", async () => {
        const pluginType = "test.dynamic-round4";
        nodeRegistry.unregisterNode(pluginType);
        const plugin = baseNode("plugin", pluginType, 20, 20, {});
        const builtin = promptNode("builtin", 20, 240);
        const model = modelNode("model", ["prompt"], 420, 80);
        const edges: CanvasConnection[] = [
            { id: "plugin-edge", fromNodeId: "plugin", fromPortId: "out", toNodeId: "model", toPortId: "prompt" },
            { id: "builtin-edge", fromNodeId: "builtin", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
        ];
        const { projectId } = await renderProject([plugin, builtin, model], edges);
        expect(document.querySelector("[data-connection-id='plugin-edge']")).toHaveAttribute("data-connection-active", "false");
        expect(screen.getByRole("button", { name: /连接：plugin out\(out\).*暂不可用：插件或端口暂不可用/ })).not.toHaveAttribute("aria-disabled");
        expect(document.querySelector("[data-connection-id='builtin-edge']")).toHaveAttribute("data-connection-active", "true");

        act(() => nodeRegistry.registerNode({ id: pluginType, version: 1, title: "动态提示词", inputs: [], outputs: [{ id: "out", provides: "prompt", label: "动态文本" }], createMetadata: () => ({}), render: () => null }));
        const portButton = await screen.findByRole("button", { name: "plugin：动态文本输出端口" });
        expect(screen.getByRole("button", { name: "连接：plugin 动态文本(out) 到 model 提示词(prompt)" })).toBeInTheDocument();
        expect(document.querySelector("[data-connection-id='plugin-edge']")).toHaveAttribute("data-connection-active", "true");
        expect(document.querySelector("[data-connection-id='builtin-edge']")).toHaveAttribute("data-connection-active", "false");
        expect(screen.getByRole("button", { name: /连接：builtin 提示词\(prompt\).*暂不可用：提示词冲突/ }).querySelector("title")).toHaveTextContent("暂不可用：提示词冲突");
        fireEvent.click(portButton);

        act(() => nodeRegistry.unregisterNode(pluginType));
        await waitFor(() => expect(screen.getByTestId("connection-status")).toHaveTextContent("连接起点已失效。"));
        expect(useCanvasStore.getState().openProject(projectId)?.connections).toEqual(edges);
    });

    it("keeps inactive incompatible and prompt-conflict edges selectable and deletable", async () => {
        const edges: CanvasConnection[] = [
            { id: "first", fromNodeId: "prompt-a", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "conflict", fromNodeId: "prompt-b", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "incompatible", fromNodeId: "image", fromPortId: "media", toNodeId: "model", toPortId: "reference_audio" },
            { id: "missing-port", fromNodeId: "prompt-a", fromPortId: "removed", toNodeId: "model", toPortId: "prompt" },
        ];
        const { projectId } = await renderProject([promptNode("prompt-a"), promptNode("prompt-b"), mediaNode("image", "image"), modelNode("model", ["prompt", "reference_audio"])], edges);
        const conflict = screen.getByRole("button", { name: /连接：prompt-b 提示词\(prompt\).*暂不可用：提示词冲突/ });
        expect(screen.getByRole("button", { name: /连接：image 媒体\(media\).*暂不可用：端口类型不兼容/ })).not.toHaveAttribute("aria-disabled");
        expect(screen.getByRole("button", { name: /连接：prompt-a removed\(removed\).*暂不可用：端口不存在或已撤销/ })).not.toHaveAttribute("aria-disabled");

        fireEvent.click(conflict);
        expect(conflict).toHaveAttribute("aria-pressed", "true");
        fireEvent.keyDown(window, { key: "Delete" });
        expect(useCanvasStore.getState().openProject(projectId)?.connections.map((edge) => edge.id)).toEqual(["first", "incompatible", "missing-port"]);
    });

    it("renders duplicate raw edges without key ambiguity and deletes exactly the selected entry", async () => {
        const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
        const edges: CanvasConnection[] = [
            { id: "same-id", fromNodeId: "prompt-a", fromPortId: "prompt", toNodeId: "model-a", toPortId: "prompt" },
            { id: "same-id", fromNodeId: "prompt-b", fromPortId: "prompt", toNodeId: "model-b", toPortId: "prompt" },
            { id: "tuple-copy", fromNodeId: "prompt-a", fromPortId: "prompt", toNodeId: "model-a", toPortId: "prompt" },
        ];
        const { projectId } = await renderProject([promptNode("prompt-a"), promptNode("prompt-b"), modelNode("model-a", ["prompt"]), modelNode("model-b", ["prompt"])], edges);
        expect(consoleError.mock.calls.flat().join(" ")).not.toContain("same key");
        const duplicateId = screen.getByRole("button", { name: /连接：prompt-b.*model-b.*暂不可用：连接 ID 重复/ });
        expect(screen.getByRole("button", { name: /连接：prompt-a.*model-a.*暂不可用：连接端口重复/ })).toBeInTheDocument();

        fireEvent.contextMenu(duplicateId, { clientX: 50, clientY: 60 });
        fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
        expect(useCanvasStore.getState().openProject(projectId)?.connections).toEqual([edges[0], edges[2]]);

        fireEvent.click(screen.getByRole("button", { name: /连接：prompt-a.*model-a.*暂不可用：连接端口重复/ }));
        fireEvent.keyDown(window, { key: "Delete" });
        expect(useCanvasStore.getState().openProject(projectId)?.connections).toEqual([edges[0]]);
    });
});
