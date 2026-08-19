import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import CanvasProjectPage from "@/pages/canvas/project";
import { getNodePorts } from "@/features/graph/connect";
import { useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { clearStorageScope, setScopedStoreFactoryForTest, setStorageScope } from "@/storage/scope";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";

afterEach(() => { cleanup(); vi.restoreAllMocks(); clearStorageScope(); setScopedStoreFactoryForTest(); useCanvasStore.setState({ projects: [], projectSyncMetadata: {}, syncNotice: null, hydrated: true }); });

it("creates an image edit node when the assigned catalog has no image generate model", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const projectId = useCanvasStore.getState().createProject("Canvas");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ models: [{
        model_id: "chiyun-gpt-image-2", service_id: "chiyun", display_name: "GPT Image 2",
        operations: ["image.edit"], input_media: ["text", "image"],
        input_ports: [
            { port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 },
            { port_id: "reference_images", media_type: "image", min_items: 1, max_items: 10 },
        ],
        parameter_mappings: {}, parameter_schema: {},
    }] }), { headers: { "content-type": "application/json" } })));
    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);

    const addImage = await screen.findByRole("button", { name: "图片生成" });
    expect(addImage).toBeEnabled();
    fireEvent.click(addImage);
    const model = useCanvasStore.getState().openProject(projectId)?.nodes.find((node) => node.metadata?.graph?.role === "model");
    expect(model?.metadata?.graph).toMatchObject({ modelId: "chiyun-gpt-image-2", operation: "image.edit" });
});

it("repairs a stored model node with missing ports once the catalog is available", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const projectId = useCanvasStore.getState().createProject("Canvas");
    const stored: CanvasNodeData = {
        id: "stored-model",
        type: CanvasNodeType.Config,
        title: "图片生成",
        position: { x: 10, y: 20 },
        width: 300,
        height: 140,
        metadata: {
            status: "idle",
            model: "demo-image-v1",
            graph: {
                schemaVersion: 1,
                role: "model",
                modelId: "demo-image-v1",
                operation: "image.generate",
                inputPorts: [],
                outputPortId: "result",
                parameters: {},
            },
        },
    };
    useCanvasStore.getState().updateProject(projectId, { nodes: [stored] });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ models: [{
        model_id: "demo-image-v1", service_id: "demo-image", display_name: "本地演示图片",
        operations: ["image.generate"], input_media: ["text", "image"],
        input_ports: [
            { port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 },
            { port_id: "reference_images", media_type: "image", min_items: 0, max_items: 4 },
        ],
        parameter_mappings: {}, parameter_schema: {},
    }] }), { headers: { "content-type": "application/json" } })));
    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);

    await waitFor(() => {
        const model = useCanvasStore.getState().openProject(projectId)?.nodes.find((node) => node.id === "stored-model");
        expect(model?.metadata?.graph?.role === "model" ? model.metadata.graph.inputPorts.map((port) => port.id) : []).toEqual(["prompt", "reference_images"]);
    });
    const repaired = useCanvasStore.getState().openProject(projectId)!.nodes.find((node) => node.id === "stored-model")!;
    expect(getNodePorts(repaired).targets.map((port) => port.portId)).toEqual(["prompt", "reference_images"]);
});

it("submits canvas image generation through jobs and writes its result node", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const projectId = useCanvasStore.getState().createProject("Canvas");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ models: [{ model_id: "real-video-looking-image", service_id: "s", display_name: "Video Model", operations: ["image.generate"], input_media: ["text"], input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }], parameter_mappings: { steps: "steps" }, parameter_schema: { steps: { type: "integer", default: 4 } } }] }), { headers: { "content-type": "application/json" } })).mockResolvedValueOnce(new Response(JSON.stringify({ id: "job-1", status: "queued" }), { status: 201, headers: { "content-type": "application/json" } })).mockResolvedValueOnce(new Response(JSON.stringify({ id: "job-1", status: "succeeded", result_url: "/api/v1/results/r-1" }), { headers: { "content-type": "application/json" } })));
    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));
    fireEvent.change(screen.getByLabelText("提示词内容"), { target: { value: "a cat" } });
    fireEvent.click(await screen.findByRole("button", { name: "图片生成" }));
    act(() => {
        const project = useCanvasStore.getState().openProject(projectId)!;
        const prompt = project.nodes.find((node) => node.metadata?.graph?.role === "prompt")!;
        const model = project.nodes.find((node) => node.metadata?.graph?.role === "model")!;
        useCanvasStore.getState().updateProject(projectId, { connections: [{ id: "prompt-model", fromNodeId: prompt.id, fromPortId: "prompt", toNodeId: model.id, toPortId: "prompt" }] });
    });
    await waitFor(() => expect(screen.getByLabelText("模型")).toHaveValue("real-video-looking-image"));
    await waitFor(() => expect(screen.getByLabelText("steps")).toHaveValue(4));
    fireEvent.change(screen.getByLabelText("steps"), { target: { value: "6" } });
    const run = screen.getByRole("button", { name: "运行模型" });
    fireEvent.click(run);
    fireEvent.click(run);
    await waitFor(() => expect(useCanvasStore.getState().openProject(projectId)?.nodes.some((node) => node.metadata?.sourceJobId === "job-1")).toBe(true));
    expect(await screen.findByTestId("result-node-job-1")).toBeVisible();
    expect(screen.getAllByTestId("result-node-job-1")).toHaveLength(1);
    const [path, request] = (fetch as any).mock.calls[1];
    expect(path).toBe("/api/v1/jobs");
    expect(request.method).toBe("POST");
    expect(JSON.parse(request.body).model_id).toBe("real-video-looking-image");
    expect(JSON.parse(request.body).params.steps).toBe(6);
    expect((fetch as any).mock.calls.filter(([path]: [string]) => path === "/api/v1/jobs")).toHaveLength(1);
    const source = useCanvasStore.getState().openProject(projectId)?.nodes.find((node) => node.type === CanvasNodeType.Config);
    const result = useCanvasStore.getState().openProject(projectId)?.nodes.find((node) => node.metadata?.sourceJobId === "job-1");
    expect(source?.metadata).toMatchObject({ status: "success", jobStatus: "succeeded", jobId: "job-1" });
    expect(screen.getByText("任务状态：已完成")).toBeVisible();
    expect(useCanvasStore.getState().openProject(projectId)?.connections).toContainEqual(expect.objectContaining({
        fromNodeId: source?.id,
        fromPortId: "result",
        toNodeId: result?.id,
        toPortId: "result",
    }));
    expect(source?.metadata?.graph).toMatchObject({
        schemaVersion: 1,
        role: "model",
        modelId: "real-video-looking-image",
        operation: "image.generate",
        inputPorts: [{ id: "prompt", accepts: "prompt" }],
        outputPortId: "result",
    });
    expect(source && getNodePorts(source).targets.map((port) => port.portId)).toEqual(["prompt"]);
    const serialized = JSON.parse(JSON.stringify(useCanvasStore.getState().openProject(projectId)!));
    useCanvasStore.getState().replaceProjects([serialized]);
    const reloadedSource = useCanvasStore.getState().openProject(projectId)?.nodes.find((node) => node.type === CanvasNodeType.Config);
    expect(reloadedSource && getNodePorts(reloadedSource).targets.map((port) => port.portId)).toEqual(["prompt"]);
});

it("submits canvas video generation through jobs and writes a video result node", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const projectId = useCanvasStore.getState().createProject("Canvas");
    vi.stubGlobal("fetch", vi.fn()
        .mockResolvedValueOnce(new Response(JSON.stringify({ models: [
            { model_id: "image-model", service_id: "images", display_name: "图片模型", operations: ["image.generate"], input_media: ["text"], input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }], parameter_mappings: {}, parameter_schema: {} },
            { model_id: "video-model", service_id: "videos", display_name: "视频模型", operations: ["video.generate"], input_media: ["text"], input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }], parameter_mappings: { duration: "duration" }, parameter_schema: { duration: { type: "integer", default: 5, minimum: 3, maximum: 8 } } },
        ] }), { headers: { "content-type": "application/json" } }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ id: "video-job-1", status: "queued" }), { status: 201, headers: { "content-type": "application/json" } }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ id: "video-job-1", operation: "video.generate", status: "succeeded", result_url: "/api/v1/results/video-job-1" }), { headers: { "content-type": "application/json" } })));
    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));
    fireEvent.change(screen.getByLabelText("提示词内容"), { target: { value: "a cloud moving slowly" } });
    fireEvent.click(await screen.findByRole("button", { name: "视频生成" }));
    act(() => {
        const project = useCanvasStore.getState().openProject(projectId)!;
        const prompt = project.nodes.find((node) => node.metadata?.graph?.role === "prompt")!;
        const model = project.nodes.find((node) => node.metadata?.graph?.role === "model")!;
        useCanvasStore.getState().updateProject(projectId, { connections: [{ id: "prompt-model", fromNodeId: prompt.id, fromPortId: "prompt", toNodeId: model.id, toPortId: "prompt" }] });
    });
    await waitFor(() => expect(screen.getByLabelText("模型")).toHaveValue("video-model"));
    fireEvent.click(screen.getByRole("button", { name: "运行模型" }));
    await waitFor(() => expect(useCanvasStore.getState().openProject(projectId)?.nodes.some((node) => node.metadata?.sourceJobId === "video-job-1")).toBe(true));
    const result = useCanvasStore.getState().openProject(projectId)?.nodes.find((node) => node.metadata?.sourceJobId === "video-job-1");
    expect(result?.type).toBe(CanvasNodeType.Video);
    expect(await screen.findByLabelText("生成视频结果")).toHaveAttribute("src", "/api/v1/results/video-job-1");
    const [, request] = (fetch as any).mock.calls[1];
    expect(JSON.parse(request.body).operation).toBe("video.generate");
    expect(JSON.parse(request.body).model_id).toBe("video-model");
});

it("runs two connected model nodes independently on the same canvas", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const projectId = useCanvasStore.getState().createProject("Concurrent canvas");
    const models = [
        { model_id: "image-model", service_id: "images", display_name: "图片模型", operations: ["image.generate"], input_media: ["text"], input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }], parameter_mappings: {}, parameter_schema: {} },
        { model_id: "video-model", service_id: "videos", display_name: "视频模型", operations: ["video.generate"], input_media: ["text"], input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }], parameter_mappings: {}, parameter_schema: {} },
    ];
    const jobByOperation = { "image.generate": "image-job", "video.generate": "video-job" } as const;
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const path = String(input);
        if (path === "/api/v1/models") return new Response(JSON.stringify({ models }), { headers: { "content-type": "application/json" } });
        if (path === "/api/v1/jobs" && init?.method === "POST") {
            const operation = JSON.parse(String(init.body)).operation as keyof typeof jobByOperation;
            return new Response(JSON.stringify({ id: jobByOperation[operation], operation, status: "queued" }), { status: 201, headers: { "content-type": "application/json" } });
        }
        const jobId = path.split("/").at(-1)!;
        const operation = jobId === "image-job" ? "image.generate" : "video.generate";
        return new Response(JSON.stringify({ id: jobId, operation, status: "succeeded", result_url: `/api/v1/results/${jobId}/0` }), { headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);

    fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));
    fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));
    const prompts = screen.getAllByLabelText("提示词内容");
    fireEvent.change(prompts[0], { target: { value: "still image" } });
    fireEvent.change(prompts[1], { target: { value: "moving image" } });
    fireEvent.click(await screen.findByRole("button", { name: "图片生成" }));
    fireEvent.click(screen.getByRole("button", { name: "视频生成" }));
    act(() => {
        const project = useCanvasStore.getState().openProject(projectId)!;
        const promptNodes = project.nodes.filter((node) => node.metadata?.graph?.role === "prompt");
        const modelNodes = project.nodes.filter((node) => node.metadata?.graph?.role === "model");
        useCanvasStore.getState().updateProject(projectId, { connections: modelNodes.map((model, index) => ({ id: `edge-${index}`, fromNodeId: promptNodes[index].id, fromPortId: "prompt", toNodeId: model.id, toPortId: "prompt" })) });
    });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "运行模型" })).toHaveLength(2));
    screen.getAllByRole("button", { name: "运行模型" }).forEach((button) => fireEvent.click(button));

    await waitFor(() => expect(useCanvasStore.getState().openProject(projectId)!.nodes.filter((node) => node.metadata?.sourceJobId)).toHaveLength(2));
    const project = useCanvasStore.getState().openProject(projectId)!;
    expect(project.nodes.filter((node) => node.metadata?.graph?.role === "model").map((node) => node.metadata?.jobStatus)).toEqual(["succeeded", "succeeded"]);
    expect(project.connections.filter((edge) => edge.toPortId === "result")).toHaveLength(2);
    expect(fetchMock.mock.calls.filter(([path, init]) => path === "/api/v1/jobs" && init?.method === "POST")).toHaveLength(2);
});

it("writes a safe failure node for a rate-limited generation", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const projectId = useCanvasStore.getState().createProject("Canvas");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ models: [{ model_id: "image", service_id: "s", display_name: "Image", operations: ["image.generate"], input_media: ["text"], input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }], parameter_mappings: {}, parameter_schema: {} }] }), { headers: { "content-type": "application/json" } })).mockResolvedValue(new Response(JSON.stringify({ code: "rate_limited", message: "raw", retryable: true, request_id: "req-1", phase: "submit" }), { status: 429, headers: { "content-type": "application/json" } })));
    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));
    fireEvent.change(screen.getByLabelText("提示词内容"), { target: { value: "private prompt" } });
    fireEvent.click(await screen.findByRole("button", { name: "图片生成" }));
    act(() => {
        const project = useCanvasStore.getState().openProject(projectId)!;
        const prompt = project.nodes.find((node) => node.metadata?.graph?.role === "prompt")!;
        const model = project.nodes.find((node) => node.metadata?.graph?.role === "model")!;
        useCanvasStore.getState().updateProject(projectId, { connections: [{ id: "prompt-model", fromNodeId: prompt.id, fromPortId: "prompt", toNodeId: model.id, toPortId: "prompt" }] });
    });
    await waitFor(() => expect(screen.getByLabelText("模型")).toHaveValue("image"));
    fireEvent.click(screen.getByRole("button", { name: "运行模型" }));
    await waitFor(() => expect(useCanvasStore.getState().openProject(projectId)?.nodes.some((node) => node.metadata?.status === "error")).toBe(true));
    expect(screen.getAllByText("请求过于频繁，请稍后重试。")).not.toHaveLength(0);
});

it("clears the queued state when an accepted job later fails", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const projectId = useCanvasStore.getState().createProject("Canvas");
    vi.stubGlobal("fetch", vi.fn()
        .mockResolvedValueOnce(new Response(JSON.stringify({ models: [{ model_id: "image", service_id: "s", display_name: "Image", operations: ["image.generate"], input_media: ["text"], input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }], parameter_mappings: {}, parameter_schema: {} }] }), { headers: { "content-type": "application/json" } }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ id: "failed-job", status: "queued" }), { status: 201, headers: { "content-type": "application/json" } }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ id: "failed-job", status: "failed", error: { code: "UPSTREAM_FAILED", message: "private upstream detail", retryable: true, request_id: "request-failed", phase: "generation" } }), { headers: { "content-type": "application/json" } })));
    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));
    fireEvent.change(screen.getByLabelText("提示词内容"), { target: { value: "safe prompt" } });
    fireEvent.click(await screen.findByRole("button", { name: "图片生成" }));
    act(() => {
        const project = useCanvasStore.getState().openProject(projectId)!;
        const prompt = project.nodes.find((node) => node.metadata?.graph?.role === "prompt")!;
        const model = project.nodes.find((node) => node.metadata?.graph?.role === "model")!;
        useCanvasStore.getState().updateProject(projectId, { connections: [{ id: "prompt-model", fromNodeId: prompt.id, fromPortId: "prompt", toNodeId: model.id, toPortId: "prompt" }] });
    });
    await waitFor(() => expect(screen.getByLabelText("模型")).toHaveValue("image"));
    fireEvent.click(screen.getByRole("button", { name: "运行模型" }));

    await waitFor(() => expect(screen.getByText("任务状态：失败，可修改后重试")).toBeVisible());
    const source = useCanvasStore.getState().openProject(projectId)!.nodes.find((node) => node.metadata?.graph?.role === "model");
    expect(source?.metadata).toMatchObject({ status: "error", jobStatus: "failed", jobId: "failed-job" });
    expect(screen.getByRole("button", { name: "运行模型" })).toBeEnabled();
});

it("restores a pending result into its source project instead of the currently open project", async () => {
    const sourceProjectId = useCanvasStore.getState().createProject("Source Canvas");
    const otherProjectId = useCanvasStore.getState().createProject("Other Canvas");
    useCanvasStore.getState().updateProject(sourceProjectId, {
        nodes: [{ id: "source-a", type: "config", title: "图片生成", position: { x: 10, y: 20 }, width: 300, height: 140, metadata: { status: "loading" } }],
    });
    setScopedStoreFactoryForTest(() => ({
        getItem: async () => [{
            jobId: "job-from-source",
            projectId: sourceProjectId,
            sourceNodeId: "source-a",
            request: { operation: "image.generate", model_id: "image", prompt: "source prompt", params: {}, asset_ids: [], idempotency_key: "source-key" },
        }],
        setItem: async () => undefined,
        removeItem: async () => undefined,
        iterate: async () => undefined,
    }) as never);
    await setStorageScope({ environment: "test", userId: "u-a" });
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
        const path = String(input);
        if (path === "/api/v1/models") return new Response(JSON.stringify({ models: [] }), { headers: { "content-type": "application/json" } });
        if (path === "/api/v1/jobs/job-from-source") return new Response(JSON.stringify({ id: "job-from-source", status: "succeeded", result_url: "/api/v1/results/source-result" }), { headers: { "content-type": "application/json" } });
        throw new Error(`unexpected request: ${path}`);
    }));

    render(<MemoryRouter initialEntries={[`/canvas/${otherProjectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);

    await waitFor(() => expect(useCanvasStore.getState().openProject(sourceProjectId)?.nodes.some((node) => node.metadata?.sourceJobId === "job-from-source")).toBe(true));
    expect(useCanvasStore.getState().openProject(otherProjectId)?.nodes.some((node) => node.metadata?.sourceJobId === "job-from-source")).toBe(false);
});

it("keeps a concurrently appended generation result when a drag frame commits", () => {
    const projectId = useCanvasStore.getState().createProject("Canvas");
    const source: CanvasNodeData = { id: "source-a", type: CanvasNodeType.Config, title: "Source", position: { x: 10, y: 20 }, width: 300, height: 140 };
    useCanvasStore.getState().updateProject(projectId, { nodes: [source] });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ models: [] }), { headers: { "content-type": "application/json" } })));
    let dragFrame: FrameRequestCallback | undefined;
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
        dragFrame = callback;
        return 1;
    });
    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);

    fireEvent.pointerDown(screen.getByTestId("draggable-node-source-a"), { button: 0, pointerId: 1, clientX: 10, clientY: 20 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 60, clientY: 70 });
    const result: CanvasNodeData = { id: "result-a", type: CanvasNodeType.Image, title: "Result", position: { x: 100, y: 120 }, width: 320, height: 180, metadata: { status: "success", sourceJobId: "job-concurrent", content: "/api/v1/results/result-a" } };
    act(() => {
        const latest = useCanvasStore.getState().openProject(projectId)!;
        useCanvasStore.getState().updateProject(projectId, { nodes: [...latest.nodes, result] });
        dragFrame?.(0);
    });

    const nodes = useCanvasStore.getState().openProject(projectId)!.nodes;
    expect(nodes.find((node) => node.id === "source-a")?.position).toEqual({ x: 60, y: 70 });
    expect(nodes.find((node) => node.id === "result-a")?.metadata?.sourceJobId).toBe("job-concurrent");
});
