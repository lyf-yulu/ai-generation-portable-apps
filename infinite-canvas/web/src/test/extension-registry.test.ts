import { expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { act, createElement } from "react";

import { listNodes, createNodeRegistry, nodeRegistry } from "@/features/nodes/registry";
import { createWorkflowRegistry, getWorkflow, workflowRegistry } from "@/features/workflows/registry";
import { ConnectionCreateMenu, NodeCreateMenu } from "@/components/canvas/canvas-create-menus";
import { portraitVideoWorkflow } from "@/features/workflows/portrait-video";

it("adds a node and workflow only through isolated registration", () => {
    const nodes = createNodeRegistry();
    const workflows = createWorkflowRegistry();
    nodes.registerNode({ id: "test.note", version: 1, title: "测试", inputs: [], outputs: ["text"], createMetadata: () => ({}), render: () => null });
    workflows.registerWorkflow({ id: "test.flow", version: 1, run: async () => ({ jobId: "job-1" }) });

    expect(nodes.listNodes().map((node) => node.id)).toEqual(["test.note"]);
    expect(workflows.getWorkflow("test.flow")?.id).toBe("test.flow");
    expect(listNodes().some((node) => node.id === "test.note")).toBe(false);
    expect(getWorkflow("test.flow")).toBeUndefined();
});

it("rejects duplicate node and workflow IDs without replacing the original", () => {
    const nodes = createNodeRegistry();
    const workflows = createWorkflowRegistry();
    const node = { id: "test.note", version: 1, title: "原始", inputs: [], outputs: [], createMetadata: () => ({}), render: () => null };
    nodes.registerNode(node);
    workflows.registerWorkflow({ id: "test.flow", version: 1, run: async () => ({ jobId: "job-1" }) });

    expect(() => nodes.registerNode({ ...node, title: "替换" })).toThrow("duplicate node: test.note");
    expect(() => workflows.registerWorkflow({ id: "test.flow", version: 2, run: async () => ({ jobId: "job-2" }) })).toThrow("duplicate workflow: test.flow");
    expect(nodes.listNodes()[0]?.title).toBe("原始");
});

it("publishes stable revisions for register and unregister lifecycle changes", () => {
    const nodes = createNodeRegistry();
    const listener = vi.fn();
    const unsubscribe = nodes.subscribe(listener);
    const initial = nodes.getSnapshot();
    nodes.registerNode({ id: "test.dynamic", version: 1, title: "Dynamic", inputs: [], outputs: [{ id: "out", provides: "image" }], createMetadata: () => ({}), render: () => null });
    const registered = nodes.getSnapshot();

    expect(registered).toBeGreaterThan(initial);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(nodes.unregisterNode("missing")).toBe(false);
    expect(nodes.getSnapshot()).toBe(registered);
    expect(nodes.unregisterNode("test.dynamic")).toBe(true);
    expect(nodes.getSnapshot()).toBeGreaterThan(registered);
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
});

it.each([
    ["duplicate inputs", [{ id: "same", accepts: "image" }, { id: "same", accepts: "video" }], []],
    ["duplicate outputs", [], [{ id: "same", provides: "image" }, { id: "same", provides: "video" }]],
    ["too many inputs", Array.from({ length: 33 }, (_, index) => `input_${index}`), []],
    ["long id", ["a".repeat(65)], []],
    ["invalid characters", ["unsafe port"], []],
    ["invalid accepts", [{ id: "input", accepts: "binary" }], []],
    ["invalid provides", [], [{ id: "output", provides: "binary" }]],
    ["unsafe label", [{ id: "input", accepts: "image", label: "bad\nlabel" }], []],
    ["long label", [], [{ id: "output", provides: "image", label: "图".repeat(65) }]],
] as const)("rejects unsafe node port declarations: %s", (_name, inputs, outputs) => {
    const nodes = createNodeRegistry();
    expect(() => nodes.registerNode({ id: "test.invalid", version: 1, title: "Invalid", inputs: inputs as never, outputs: outputs as never, createMetadata: () => ({}), render: () => null })).toThrow("invalid node port declaration");
    expect(nodes.listNodes()).toEqual([]);
});

it("does not expose mutable registry collections and returns undefined for unknown workflows", () => {
    const nodes = createNodeRegistry();
    nodes.registerNode({ id: "test.note", version: 1, title: "测试", inputs: [], outputs: [], createMetadata: () => ({}), render: () => null });
    const listed = nodes.listNodes();
    expect(() => (listed as unknown[]).pop()).toThrow();

    expect(nodes.listNodes()).toHaveLength(1);
    expect(getWorkflow("unknown.workflow")).toBeUndefined();
});

it("defensively freezes nested node data instead of retaining caller-owned objects", () => {
    const nodes = createNodeRegistry();
    const size = { width: 320, height: 200 };
    const inputs = [{ id: "reference", accepts: "image" as const }];
    const outputs = [{ id: "clip", provides: "video" as const }];
    nodes.registerNode({ id: "test.sized", version: 1, title: "测试", inputs, outputs, defaultSize: size, createMetadata: () => ({}), render: () => null });
    size.width = 999;
    inputs[0].accepts = "audio" as never;
    outputs[0].provides = "image" as never;
    const stored = nodes.getNode("test.sized");
    try { (stored?.defaultSize as { width: number }).width = 888; } catch { /* frozen definitions may throw in strict mode */ }

    expect(nodes.getNode("test.sized")?.defaultSize).toEqual({ width: 320, height: 200 });
    expect(nodes.getNode("test.sized")?.inputs).toEqual([{ id: "reference", accepts: "image" }]);
    expect(nodes.getNode("test.sized")?.outputs).toEqual([{ id: "clip", provides: "video" }]);
    expect(nodes.listNodes()[0]?.defaultSize).toEqual({ width: 320, height: 200 });
});

it("keeps isolated registries empty and rejects every duplicate ID", () => {
    const nodes = createNodeRegistry();
    const workflows = createWorkflowRegistry();
    const node = { id: "test.builtin", version: 1, title: "原始", inputs: ["image"], outputs: ["video"], defaultSize: { width: 320, height: 200 }, createMetadata: () => ({}), render: () => null };
    const workflow = { id: "test.builtin.workflow", version: 1, run: async () => ({ jobId: "job" }) };
    expect(nodes.listNodes()).toEqual([]);
    nodes.registerNode(node);
    workflows.registerWorkflow(workflow);
    expect(() => nodes.registerNode({ ...node })).toThrow("duplicate node: test.builtin");
    expect(() => nodes.registerNode({ ...node, title: "冲突" })).toThrow("duplicate node: test.builtin");
    expect(() => workflows.registerWorkflow({ ...workflow })).toThrow("duplicate workflow: test.builtin.workflow");
    expect(() => workflows.registerWorkflow({ ...workflow, run: async () => ({}) })).toThrow("duplicate workflow: test.builtin.workflow");
});

it("initializes singleton registries with built-ins exactly once", () => {
    expect(nodeRegistry.getNode("text")?.version).toBe(1);
    expect(workflowRegistry.getWorkflow("portrait.video")?.version).toBe(1);
    expect(nodeRegistry.listNodes().filter((node) => node.id === "text")).toHaveLength(1);
});

it("recreates singleton registries with one built-in set after module reset", async () => {
    vi.resetModules();
    const freshNodes = await import("@/features/nodes/registry");
    const freshWorkflows = await import("@/features/workflows/registry");
    expect(freshNodes.nodeRegistry.listNodes().filter((node) => node.id === "text")).toHaveLength(1);
    expect(freshWorkflows.workflowRegistry.getWorkflow("portrait.video")?.id).toBe("portrait.video");
});

it("updates both mounted create menus after an isolated registry registration", () => {
    const registry = createNodeRegistry();
    const props = { onCreate: () => undefined, onClose: () => undefined };
    render(createElement("div", undefined,
        createElement(ConnectionCreateMenu, { ...props, registry, pending: { connection: { nodeId: "n", handleType: "source" }, position: { x: 0, y: 0 } } }),
        createElement(NodeCreateMenu, { ...props, registry, position: { x: 0, y: 0 } }),
    ));
    act(() => registry.registerNode({ id: "test.live", version: 1, title: "实时节点", connectionTitle: "实时生成", inputs: [], outputs: [], createMetadata: () => ({}), render: () => null }));
    expect(screen.getByText("实时生成")).toBeInTheDocument();
    expect(screen.getByText("实时节点")).toBeInTheDocument();
});

it("runs portrait video as upload, active asset, then generic image-to-video submission", async () => {
    const calls: string[] = [];
    const asset = await portraitVideoWorkflow.run({
        file: new File(["image"], "portrait.png", { type: "image/png" }),
        modelId: "video-model-a",
        prompt: "walk forward",
        params: { seconds: 5 },
        idempotencyKey: "portrait-1",
        uploadAsset: async (file, kind) => {
            calls.push(`upload:${file.name}:${kind}`);
            return { id: "asset-1", kind: "portrait", status: "processing", mime_type: "image/png" };
        },
        fetchAsset: async () => {
            calls.push("asset");
            return { id: "asset-1", kind: "portrait", status: "active", mime_type: "image/png" };
        },
        submitJob: async (request) => {
            calls.push(`submit:${request.operation}:${request.model_id}`);
            expect(request.asset_ids).toEqual(["asset-1"]);
            expect(request).not.toHaveProperty("service_id");
            return { jobId: "job-1" };
        },
        sleep: async () => undefined,
    });

    expect(calls).toEqual(["upload:portrait.png:portrait", "asset", "submit:video.image_to_video:video-model-a"]);
    expect(asset).toEqual({ jobId: "job-1", assetId: "asset-1" });
});

it("stops portrait workflow when the asset fails or remains pending past its timeout", async () => {
    const base = {
        file: new File(["image"], "portrait.png", { type: "image/png" }),
        modelId: "video-model-a",
        prompt: "walk forward",
        params: {},
        idempotencyKey: "portrait-1",
        uploadAsset: async () => ({ id: "asset-1", kind: "portrait" as const, status: "processing" as const, mime_type: "image/png" }),
        submitJob: async () => ({ jobId: "job-1" }),
        sleep: async () => undefined,
    };
    await expect(portraitVideoWorkflow.run({ ...base, fetchAsset: async () => ({ id: "asset-1", kind: "portrait", status: "failed", mime_type: "image/png" }) })).rejects.toMatchObject({ phase: "asset-poll", assetId: "asset-1" });
    await expect(portraitVideoWorkflow.run({ ...base, fetchAsset: async () => ({ id: "asset-1", kind: "portrait", status: "processing", mime_type: "image/png" }), pollIntervalMs: 1, maxWaitMs: 1 })).rejects.toMatchObject({ phase: "asset-poll", assetId: "asset-1" });
});

it("rejects invalid portrait polling limits before upload or sleep", async () => {
    const uploadAsset = async () => { throw new Error("upload should not run"); };
    const input = {
        file: new File(["image"], "portrait.png", { type: "image/png" }), modelId: "video-model-a", prompt: "walk", params: {}, idempotencyKey: "portrait-1",
        uploadAsset, fetchAsset: async () => ({ id: "asset", kind: "portrait" as const, status: "processing" as const, mime_type: "image/png" }), submitJob: async () => ({ jobId: "job" }), sleep: async () => { throw new Error("sleep should not run"); },
    };
    for (const pollIntervalMs of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
        await expect(portraitVideoWorkflow.run({ ...input, pollIntervalMs })).rejects.toThrow("pollIntervalMs must be a finite positive number");
    }
    for (const maxWaitMs of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
        await expect(portraitVideoWorkflow.run({ ...input, maxWaitMs })).rejects.toThrow("maxWaitMs must be a finite positive number");
    }
});

it("bounds portrait polling when the maximum wait is shorter than the interval", async () => {
    let sleeps = 0;
    let assetReads = 0;
    await expect(portraitVideoWorkflow.run({
        file: new File(["image"], "portrait.png", { type: "image/png" }), modelId: "video-model-a", prompt: "walk", params: {}, idempotencyKey: "portrait-1",
        uploadAsset: async () => ({ id: "asset", kind: "portrait", status: "processing", mime_type: "image/png" }),
        fetchAsset: async () => { assetReads += 1; return { id: "asset", kind: "portrait" as const, status: "processing" as const, mime_type: "image/png" }; },
        submitJob: async () => ({ jobId: "job" }), sleep: async () => { sleeps += 1; }, pollIntervalMs: 10, maxWaitMs: 1,
    })).rejects.toMatchObject({ phase: "asset-poll", assetId: "asset" });
    expect(assetReads).toBe(0);
    expect(sleeps).toBe(0);
});
