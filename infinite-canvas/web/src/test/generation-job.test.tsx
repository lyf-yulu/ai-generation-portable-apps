import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { clearStorageScope, setScopedStoreFactoryForTest, setStorageScope } from "@/storage/scope";
import { useGenerationJob } from "@/features/generation/use-generation-job";
import { clearGenerationTasks, useGenerationTasks } from "@/features/generation/use-generation-job";
import { TaskTray } from "@/components/layout/task-tray";
import { appendJobResults, appendResultNode, createResultNode } from "@/features/generation/result-node";
import type { CanvasNodeData } from "@/types/canvas";

afterEach(() => {
    cleanup();
    clearStorageScope();
    setScopedStoreFactoryForTest();
    clearGenerationTasks();
});

it("resumes an existing job without submitting another job", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const api = {
        create: vi.fn(),
        fetch: vi.fn().mockResolvedValue({ id: "j-1", status: "succeeded", result_url: "/api/v1/results/r-1" }),
    };
    const { result } = renderHook(() => useGenerationJob({ api: api as any, pollDelayMs: 1 }));

    await act(async () => result.current.resume("j-1"));
    await waitFor(() => expect(result.current.state.status).toBe("succeeded"));
    expect(api.create).not.toHaveBeenCalled();
    expect(api.fetch).toHaveBeenCalledWith("j-1", expect.any(Object));
});

it("cancels a queued job through the server and stops local polling", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    let pollSignal: AbortSignal | undefined;
    const api = {
        create: vi.fn(),
        fetch: vi.fn((_id: string, signal?: AbortSignal) => { pollSignal = signal; return new Promise(() => undefined); }),
        cancel: vi.fn().mockResolvedValue({ id: "j-queued", status: "failed", error: { code: "TASK_CANCELLED", message: "The job was cancelled.", retryable: false, request_id: "r", phase: "generation" } }),
    };
    const onCancelled = vi.fn();
    const { result } = renderHook(() => useGenerationJob({ api: api as any, onCancelled }));
    void result.current.resume("j-queued");
    await waitFor(() => expect(api.fetch).toHaveBeenCalled());

    await act(async () => result.current.cancelQueued("j-queued"));
    expect(api.cancel).toHaveBeenCalledWith("j-queued");
    expect(pollSignal?.aborted).toBe(true);
    expect(result.current.state).toMatchObject({ status: "failed", jobId: "j-queued", retryable: false });
    expect(onCancelled).toHaveBeenCalledWith(expect.objectContaining({ jobId: "j-queued" }));
});

it("preserves a success that wins the cancellation race", async () => {
    const saved = { jobId: "j-race", projectId: "project-a", sourceNodeId: "model-a", request: { operation: "image.generate" as const, model_id: "m", prompt: "p", params: {}, asset_ids: [], idempotency_key: "key" } };
    setScopedStoreFactoryForTest(() => ({ getItem: async () => [saved], setItem: async () => undefined, removeItem: async () => undefined, iterate: async () => undefined }) as never);
    await setStorageScope({ environment: "test", userId: "u-a" });
    const api = {
        create: vi.fn(),
        fetch: vi.fn(() => new Promise(() => undefined)),
        cancel: vi.fn().mockResolvedValue({ id: "j-race", status: "succeeded", result_url: "/api/v1/results/j-race" }),
    };
    const onSucceeded = vi.fn();
    const onCancelled = vi.fn();
    const { result } = renderHook(() => useGenerationJob({ api: api as any, onSucceeded, onCancelled }));
    await waitFor(() => expect(api.fetch).toHaveBeenCalled());

    await act(async () => result.current.cancelQueued("j-race"));
    expect(result.current.state.status).toBe("succeeded");
    expect(onSucceeded).toHaveBeenCalledWith(expect.objectContaining({ id: "j-race", operation: "image.generate" }), expect.objectContaining({ projectId: "project-a", sourceNodeId: "model-a" }));
    expect(onCancelled).not.toHaveBeenCalled();
});

it.each(["queued", "running"] as const)("keeps polling when cancellation returns %s", async (status) => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    let pollSignal: AbortSignal | undefined;
    const api = {
        create: vi.fn(),
        fetch: vi.fn((_id: string, signal?: AbortSignal) => { pollSignal = signal; return new Promise(() => undefined); }),
        cancel: vi.fn().mockResolvedValue({ id: "j-active", status }),
    };
    const onCancelled = vi.fn();
    const { result } = renderHook(() => useGenerationJob({ api: api as any, onCancelled }));
    void result.current.resume("j-active");
    await waitFor(() => expect(api.fetch).toHaveBeenCalled());

    await act(async () => result.current.cancelQueued("j-active"));
    expect(pollSignal?.aborted).toBe(false);
    expect(result.current.state.status).toBe(status);
    expect(onCancelled).not.toHaveBeenCalled();
});

it("reuses the pending idempotency key after an ambiguous submit failure", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const api = {
        create: vi.fn().mockRejectedValueOnce(new TypeError("network")).mockResolvedValue({ id: "j-1", status: "queued" }),
        fetch: vi.fn().mockResolvedValue({ id: "j-1", status: "succeeded", result_url: "/api/v1/results/r-1" }),
    };
    const { result } = renderHook(() => useGenerationJob({ api, pollDelayMs: 1, idempotencyKey: () => "stable-key" }));
    const request = { operation: "image.generate" as const, model_id: "m", prompt: "p", params: {}, asset_ids: [], projectId: "project-a" };

    await act(async () => expect(result.current.submit(request)).rejects.toThrow("network"));
    await act(async () => result.current.submit(request));
    expect(api.create.mock.calls.map(([job]) => job.idempotency_key)).toEqual(["stable-key", "stable-key"]);
});

it("restores this user's saved job references and only polls them", async () => {
    setScopedStoreFactoryForTest(
        () =>
            ({
                getItem: async () => [{ jobId: "j-saved", request: { operation: "video.generate", model_id: "m", prompt: "p", params: {}, asset_ids: [], idempotency_key: "key" } }],
                setItem: async () => undefined,
                removeItem: async () => undefined,
                iterate: async () => undefined,
            }) as never,
    );
    await setStorageScope({ environment: "test", userId: "u-a" });
    const api = { create: vi.fn(), fetch: vi.fn().mockResolvedValue({ id: "j-saved", status: "succeeded", result_url: "/api/v1/results/r" }) };
    renderHook(() => useGenerationJob({ api: api as any, pollDelayMs: 1 }));
    await waitFor(() => expect(api.fetch).toHaveBeenCalledWith("j-saved", expect.any(Object)));
    expect(api.create).not.toHaveBeenCalled();
});

it("polls two resumed jobs independently", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    let first!: (value: any) => void;
    let second!: (value: any) => void;
    const api = {
        create: vi.fn(),
        fetch: vi.fn(
            (id: string) =>
                new Promise((resolve) => {
                    if (id === "j-1") first = resolve;
                    else second = resolve;
                }),
        ),
    };
    const { result } = renderHook(() => useGenerationJob({ api: api as any }));
    void result.current.resume("j-1");
    void result.current.resume("j-2");
    await waitFor(() => expect(api.fetch).toHaveBeenCalledTimes(2));
    await act(async () => {
        first({ id: "j-1", status: "succeeded", result_url: "/api/v1/results/a" });
        second({ id: "j-2", status: "succeeded", result_url: "/api/v1/results/b" });
    });
    expect(api.fetch).toHaveBeenCalledWith("j-1", expect.any(Object));
    expect(api.fetch).toHaveBeenCalledWith("j-2", expect.any(Object));
});

it("publishes each concurrent job independently to the shared task tray", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const resolvers = new Map<string, (value: any) => void>();
    const api = { create: vi.fn(), fetch: vi.fn((id: string) => new Promise((resolve) => resolvers.set(id, resolve))) };
    const { result } = renderHook(() => useGenerationJob({ api: api as any }));
    render(<TaskTray />);

    void result.current.resume("job-image");
    void result.current.resume("job-second");
    await waitFor(() =>
        expect(
            useGenerationTasks
                .getState()
                .tasks.map((task) => task.jobId)
                .sort(),
        ).toEqual(["job-image", "job-second"]),
    );
    expect(screen.getByText("job-image")).toBeVisible();
    expect(screen.getByText("job-second")).toBeVisible();

    await act(async () => resolvers.get("job-image")?.({ id: "job-image", status: "succeeded", result_url: "/api/v1/results/a" }));
    expect(useGenerationTasks.getState().tasks.find((task) => task.jobId === "job-image")?.status).toBe("succeeded");
    expect(useGenerationTasks.getState().tasks.find((task) => task.jobId === "job-second")?.status).not.toBe("succeeded");
});

it("keeps an ambiguous saved submission dormant until a manual retry reuses its key", async () => {
    setScopedStoreFactoryForTest(
        () =>
            ({
                getItem: async () => [{ request: { operation: "image.generate", model_id: "m", prompt: "p", params: {}, asset_ids: [], idempotency_key: "accepted-key" } }],
                setItem: async () => undefined,
                removeItem: async () => undefined,
                iterate: async () => undefined,
            }) as never,
    );
    await setStorageScope({ environment: "test", userId: "u-a" });
    const api = { create: vi.fn().mockResolvedValue({ id: "j-1", status: "succeeded", result_url: "/api/v1/results/r" }), fetch: vi.fn() };
    const { result } = renderHook(() => useGenerationJob({ api: api as any }));
    await waitFor(() => expect(api.create).not.toHaveBeenCalled());
    await act(async () => result.current.retry("accepted-key"));
    expect(api.create.mock.calls[0][0].idempotency_key).toBe("accepted-key");
});

it("cancels an old scope poll and never publishes it into a new scope", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    let resolve!: (job: { id: string; status: "succeeded"; result_url: string }) => void;
    const api = {
        create: vi.fn(),
        fetch: vi.fn(
            () =>
                new Promise((done) => {
                    resolve = done;
                }),
        ),
    };
    const { result } = renderHook(() => useGenerationJob({ api: api as any, pollDelayMs: 1 }));
    void act(async () => result.current.resume("j-a"));
    await waitFor(() => expect(api.fetch).toHaveBeenCalled());
    await setStorageScope({ environment: "test", userId: "u-b" });
    await act(async () => resolve({ id: "j-a", status: "succeeded", result_url: "/api/v1/results/r-a" }));
    expect(result.current.state.status).not.toBe("succeeded");
});

it("creates typed same-origin result nodes once with a safe source offset", () => {
    const source = { id: "source", type: "text", title: "source", position: { x: 10, y: 20 }, width: 100, height: 100 };
    const image = createResultNode({ id: "image-job", operation: "image.generate", status: "succeeded", result_url: "/api/v1/results/image" }, source);
    const video = createResultNode({ id: "video-job", operation: "video.generate", status: "succeeded", result_url: "/api/v1/results/video" });
    expect(image).toMatchObject({
        type: "image",
        position: { x: 58, y: 68 },
        metadata: { content: "/api/v1/results/image", sourceJobId: "image-job", graph: { role: "result", inputPortId: "result", outputPortId: "media", mediaType: "image", jobId: "image-job" } },
    });
    expect(video).toMatchObject({ metadata: { graph: { role: "result", inputPortId: "result", outputPortId: "media", mediaType: "video", jobId: "video-job" } } });
    expect(video).toMatchObject({ type: "video", position: { x: 80, y: 80 } });
    expect(appendResultNode([image], { id: "image-job", operation: "image.generate", status: "succeeded", result_url: "/api/v1/results/image" }, source)).toHaveLength(1);
});

it("creates one reusable result node per protected multi-result item", () => {
    const source = { id: "source", type: "config", title: "source", position: { x: 10, y: 20 }, width: 100, height: 100 };
    const job = {
        id: "multi-job",
        operation: "image.generate" as const,
        status: "succeeded" as const,
        results: [
            { url: "/api/v1/results/multi-job/0", asset_id: "job-result.multi-job.0", media_type: "image" as const },
            { url: "/api/v1/results/multi-job/1", asset_id: "job-result.multi-job.1", media_type: "image" as const },
        ],
    };
    const nodes = appendResultNode([], job, source);
    expect(nodes).toHaveLength(2);
    expect(nodes.map((node) => node.metadata?.graph)).toEqual([
        expect.objectContaining({ role: "result", assetId: "job-result.multi-job.0", mediaType: "image" }),
        expect.objectContaining({ role: "result", assetId: "job-result.multi-job.1", mediaType: "image" }),
    ]);
    expect(appendResultNode(nodes, job, source)).toHaveLength(2);
});

it("creates idempotent model-to-result connections and repairs a missing edge", () => {
    const source: CanvasNodeData = {
        id: "model-a",
        type: "config",
        title: "图片生成",
        position: { x: 20, y: 30 },
        width: 340,
        height: 300,
        metadata: {
            status: "success",
            graph: { schemaVersion: 1, role: "model", modelId: "image", operation: "image.generate", inputPorts: [{ id: "prompt", accepts: "prompt" }], outputPortId: "result", parameters: {} },
        },
    };
    const job = { id: "job-linked", operation: "image.generate" as const, status: "succeeded" as const, result_url: "/api/v1/results/job-linked/0" };
    let sequence = 0;
    const createId = () => `created-${++sequence}`;

    const first = appendJobResults([source], [], job, source, createId);
    const result = first.nodes.find((node) => node.metadata?.sourceJobId === job.id)!;
    expect(first.connections).toEqual([{ id: "created-2", fromNodeId: source.id, fromPortId: "result", toNodeId: result.id, toPortId: "result" }]);

    const repeated = appendJobResults(first.nodes, first.connections, job, source, createId);
    expect(repeated).toEqual(first);

    const repaired = appendJobResults(first.nodes, [], job, source, createId);
    expect(repaired.nodes).toEqual(first.nodes);
    expect(repaired.connections).toEqual([{ id: "created-3", fromNodeId: source.id, fromPortId: "result", toNodeId: result.id, toPortId: "result" }]);
});
