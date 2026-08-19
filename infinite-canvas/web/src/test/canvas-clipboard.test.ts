import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { clearCanvasClipboard, copyCanvasSelection, pasteCanvasSelection } from "@/features/graph/canvas-clipboard";
import { clearStorageScope, setStorageScope } from "@/storage/scope";
import type { CanvasProject } from "@/stores/canvas/use-canvas-store";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from "@/types/canvas";

function prompt(id: string, x: number): CanvasNodeData {
    return {
        id,
        type: CanvasNodeType.Text,
        title: `提示词 ${id}`,
        position: { x, y: 40 },
        width: 300,
        height: 250,
        metadata: { content: id, status: "idle", graph: { schemaVersion: 1, role: "prompt", text: id, outputPortId: "prompt" } },
    };
}

function model(id: string, x: number): CanvasNodeData {
    return {
        id,
        type: CanvasNodeType.Config,
        title: `模型 ${id}`,
        position: { x, y: 100 },
        width: 340,
        height: 360,
        metadata: {
            status: "loading",
            jobId: "paid-job",
            jobStatus: "running",
            idempotencyKey: "paid-key",
            requestId: "request-secret",
            phase: "provider",
            graph: { schemaVersion: 1, role: "model", modelId: "seedream", operation: "image.generate", inputPorts: [{ id: "prompt", accepts: "prompt" }], outputPortId: "result", parameters: { size: "1024x1024" } },
        },
    };
}

function project(nodes: CanvasNodeData[], connections: CanvasConnection[] = []): CanvasProject {
    return {
        id: "project",
        title: "Clipboard",
        createdAt: "2026-08-11T00:00:00.000Z",
        updatedAt: "2026-08-11T00:00:00.000Z",
        nodes,
        connections,
        chatSessions: [],
        activeChatId: null,
        backgroundMode: "lines",
        showImageInfo: false,
        viewport: { x: 0, y: 0, k: 1 },
        graphSchemaVersion: 1,
    };
}

beforeEach(async () => {
    clearStorageScope();
    clearCanvasClipboard();
    await setStorageScope({ environment: "test", userId: "clipboard-user" });
});

afterEach(() => {
    clearCanvasClipboard();
    clearStorageScope();
});

describe("scoped canvas clipboard", () => {
    it("copies only selected nodes and their internal edges into an immutable snapshot", () => {
        const source = project([prompt("prompt", 10), model("model", 400), model("outside", 800)], [
            { id: "inside", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "model", toPortId: "prompt" },
            { id: "outside", fromNodeId: "prompt", fromPortId: "prompt", toNodeId: "outside", toPortId: "prompt" },
        ]);

        expect(copyCanvasSelection(source, new Set(["prompt", "model"]))).toEqual({ ok: true, nodeCount: 2 });
        source.nodes[0].title = "外部修改";
        source.connections[0].toPortId = "changed";
        const emptyTarget = project([]);
        const ids = ["node-a", "node-b", "edge-a"][Symbol.iterator]();
        const pasted = pasteCanvasSelection(emptyTarget, () => ids.next().value!);

        expect(pasted.ok).toBe(true);
        if (!pasted.ok) return;
        expect(pasted.nodes.map((item) => ({ id: item.id, title: item.title, position: item.position }))).toEqual([
            { id: "node-a", title: "提示词 prompt", position: { x: 42, y: 72 } },
            { id: "node-b", title: "模型 model", position: { x: 432, y: 132 } },
        ]);
        expect(pasted.connections).toEqual([{ id: "edge-a", fromNodeId: "node-a", fromPortId: "prompt", toNodeId: "node-b", toPortId: "prompt" }]);
    });

    it("uses fresh IDs and an increasing offset on every paste", () => {
        const source = project([model("model", 100)]);
        copyCanvasSelection(source, new Set(["model"]));
        let sequence = 0;
        const createId = () => `id-${++sequence}`;

        const first = pasteCanvasSelection(project([]), createId);
        const second = pasteCanvasSelection(project([]), createId);

        expect(first.ok && first.nodes[0].position).toEqual({ x: 132, y: 132 });
        expect(second.ok && second.nodes[0].position).toEqual({ x: 164, y: 164 });
        expect(first.ok && first.nodes[0].id).not.toBe(second.ok && second.nodes[0].id);
    });

    it("clears paid task lifecycle fields while preserving model parameters", () => {
        copyCanvasSelection(project([model("model", 100)]), new Set(["model"]));
        const pasted = pasteCanvasSelection(project([]), () => "new-model");

        expect(pasted.ok).toBe(true);
        if (!pasted.ok) return;
        expect(pasted.nodes[0].metadata).toMatchObject({ status: "idle", graph: { role: "model", parameters: { size: "1024x1024" } } });
        expect(pasted.nodes[0].metadata).not.toHaveProperty("jobId");
        expect(pasted.nodes[0].metadata).not.toHaveProperty("jobStatus");
        expect(pasted.nodes[0].metadata).not.toHaveProperty("idempotencyKey");
        expect(pasted.nodes[0].metadata).not.toHaveProperty("requestId");
        expect(pasted.nodes[0].metadata).not.toHaveProperty("phase");
    });

    it("allows prompt copies alongside other prompts but still enforces project bounds", () => {
        copyCanvasSelection(project([prompt("copied", 10)]), new Set(["copied"]));
        const pastedPrompt = pasteCanvasSelection(project([prompt("existing", 20)]), () => "new");
        expect(pastedPrompt.ok).toBe(true);
        if (pastedPrompt.ok) expect(pastedPrompt.nodes[0].metadata?.graph).toMatchObject({ role: "prompt" });

        clearCanvasClipboard();
        copyCanvasSelection(project([model("copied", 10)]), new Set(["copied"]));
        const full = project(Array.from({ length: 1000 }, (_, index) => model(`existing-${index}`, index)));
        expect(pasteCanvasSelection(full, () => "new")).toEqual({ ok: false, reason: "node-limit" });
    });

    it("invalidates clipboard contents immediately when the user scope changes", async () => {
        copyCanvasSelection(project([model("model", 10)]), new Set(["model"]));
        clearStorageScope();
        await setStorageScope({ environment: "test", userId: "other-user" });

        expect(pasteCanvasSelection(project([]), () => "new")).toEqual({ ok: false, reason: "empty" });
    });
});
