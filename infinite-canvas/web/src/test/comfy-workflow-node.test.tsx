import { expect, it } from "vitest";

import { getNodePorts } from "@/features/graph/connect";
import { GRAPH_SCHEMA_VERSION } from "@/features/graph/contracts";
import { normalizeCanvasProject } from "@/features/graph/normalize-project";
import { createComfyWorkflowNode } from "@/features/nodes/comfy-workflow";
import { nodeRegistry } from "@/features/nodes/registry";

it("registers one generic ComfyUI node without registering imported node types", () => {
    expect(nodeRegistry.getNode("comfy.workflow")).toMatchObject({
        title: "ComfyUI 工作流",
        version: 1,
        showInCreateMenu: true,
    });
    expect(nodeRegistry.getNode("MiniMaxH3ImageToVideo")).toBeUndefined();
});

it("preserves a selected template revision and leaves execution disabled", () => {
    const node = createComfyWorkflowNode({
        workflowId: "wf-1",
        revision: 2,
        title: "Core",
        inputs: [{ id: "prompt", accepts: "prompt" }],
        executionEnabled: false,
    });

    expect(node.metadata?.graph).toMatchObject({
        schemaVersion: GRAPH_SCHEMA_VERSION,
        role: "comfy-workflow",
        workflowId: "wf-1",
        workflowRevision: 2,
        inputPorts: [{ id: "prompt", accepts: "prompt" }],
        outputPortId: "result",
        executionEnabled: false,
    });
    expect(node.title).toBe("Core");
    expect(getNodePorts(node)).toEqual({
        sources: [expect.objectContaining({ portId: "result", valueType: "result" })],
        targets: [expect.objectContaining({ portId: "prompt", valueType: "prompt" })],
    });
});

it("normalizes ComfyUI workflow metadata without retaining non-project workflow fields", () => {
    const node = createComfyWorkflowNode({
        workflowId: "wf-1",
        revision: 2,
        title: "Core",
        inputs: [],
        executionEnabled: false,
    });
    const project = normalizeCanvasProject({
        id: "project",
        title: "Project",
        createdAt: "2026-08-16T00:00:00.000Z",
        updatedAt: "2026-08-16T00:00:00.000Z",
        nodes: [{ ...node, metadata: { ...node.metadata, graph: { ...node.metadata!.graph!, rawJson: { class_type: "KSampler" }, serviceUrl: "https://example.invalid", credential: "secret" } } }],
        connections: [],
        chatSessions: [],
        activeChatId: null,
        backgroundMode: "lines",
        showImageInfo: false,
        viewport: { x: 0, y: 0, k: 1 },
    });

    expect(project.nodes[0]?.metadata?.graph).toEqual({
        schemaVersion: GRAPH_SCHEMA_VERSION,
        role: "comfy-workflow",
        workflowId: "wf-1",
        workflowRevision: 2,
        inputPorts: [],
        outputPortId: "result",
        executionEnabled: false,
    });
});

it("normalizes model metadata without retaining endpoint, credential, or ComfyUI implementation fields", () => {
    const project = normalizeCanvasProject({
        id: "project",
        title: "Project",
        createdAt: "2026-08-16T00:00:00.000Z",
        updatedAt: "2026-08-16T00:00:00.000Z",
        nodes: [{
            id: "model", type: "config", title: "Model", position: { x: 0, y: 0 }, width: 320, height: 200,
            metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: "model-1", operation: "image.generate", inputPorts: [], outputPortId: "result", parameters: {}, endpoint: "https://example.invalid", credential: "secret", class_type: "KSampler" } },
        }],
        connections: [], chatSessions: [], activeChatId: null, backgroundMode: "lines" as const, showImageInfo: false, viewport: { x: 0, y: 0, k: 1 },
    });

    expect(project.nodes[0]?.metadata?.graph).toEqual({
        schemaVersion: GRAPH_SCHEMA_VERSION,
        role: "model",
        modelId: "model-1",
        operation: "image.generate",
        inputPorts: [],
        outputPortId: "result",
        parameters: {},
    });
});

it("drops malicious outer ComfyUI metadata across normalize/save/reload while retaining only the validated template reference", () => {
    const submitted = {
        id: "project",
        title: "Project",
        createdAt: "2026-08-16T00:00:00.000Z",
        updatedAt: "2026-08-16T00:00:00.000Z",
        nodes: [{
            id: "workflow", type: "comfy.workflow", title: "Core", position: { x: 0, y: 0 }, width: 320, height: 200,
            metadata: {
                status: "idle",
                graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "comfy-workflow", workflowId: "wf-1", workflowRevision: 2, inputPorts: [], outputPortId: "result", executionEnabled: false },
                endpoint: "https://example.invalid", base_url: "https://example.invalid", service_url: "https://example.invalid",
                credential: "secret", credentials: "secret", auth: "secret", token: "secret", headers: { authorization: "secret" },
                class_type: "KSampler", node_ids: ["1"], code: "run()", plugin: "untrusted",
            } as never,
        }],
        connections: [], chatSessions: [], activeChatId: null, backgroundMode: "lines" as const, showImageInfo: false, viewport: { x: 0, y: 0, k: 1 },
    };

    const saved = normalizeCanvasProject(submitted);
    const reloaded = normalizeCanvasProject(JSON.parse(JSON.stringify(saved)));

    expect(reloaded.nodes[0]?.metadata).toEqual({
        status: "idle",
        graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "comfy-workflow", workflowId: "wf-1", workflowRevision: 2, inputPorts: [], outputPortId: "result", executionEnabled: false },
    });
    expect(JSON.stringify(reloaded)).not.toMatch(/example\.invalid|secret|KSampler|run\(\)|untrusted/);
});

it("projects nested ComfyUI and model input ports to approved descriptors across normalize/save/reload", () => {
    const inputPort = {
        id: "prompt",
        accepts: "prompt",
        label: "提示词",
        endpoint: "https://example.invalid",
        credential: "secret",
        class_type: "KSampler",
        code: "run()",
        plugin: "untrusted",
        headers: { authorization: "secret" },
    };
    const project = normalizeCanvasProject({
        id: "project", title: "Project", createdAt: "2026-08-16T00:00:00.000Z", updatedAt: "2026-08-16T00:00:00.000Z",
        nodes: [
            { id: "workflow", type: "comfy.workflow", title: "Core", position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "comfy-workflow", workflowId: "wf-1", workflowRevision: 2, inputPorts: [inputPort], outputPortId: "result", executionEnabled: false } } as never },
            { id: "model", type: "config", title: "Model", position: { x: 400, y: 0 }, width: 320, height: 200, metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: "model-1", operation: "image.generate", inputPorts: [inputPort], outputPortId: "result", parameters: {} } } as never },
        ],
        connections: [], chatSessions: [], activeChatId: null, backgroundMode: "lines" as const, showImageInfo: false, viewport: { x: 0, y: 0, k: 1 },
    });
    const reloaded = normalizeCanvasProject(JSON.parse(JSON.stringify(project)));

    const inputPorts = reloaded.nodes.map((node) => {
        const graph = node.metadata?.graph;
        if (!graph || (graph.role !== "comfy-workflow" && graph.role !== "model")) throw new Error("expected workflow input ports");
        return graph.inputPorts;
    });
    expect(inputPorts).toEqual([
        [{ id: "prompt", accepts: "prompt", label: "提示词" }],
        [{ id: "prompt", accepts: "prompt", label: "提示词" }],
    ]);
    expect(JSON.stringify(reloaded)).not.toMatch(/example\.invalid|secret|KSampler|run\(\)|untrusted/);
});
