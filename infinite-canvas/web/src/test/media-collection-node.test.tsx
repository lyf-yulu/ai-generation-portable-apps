import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { setCsrfToken } from "@/api/client";
import { deleteMediaAsset, fetchAsset, uploadMediaAsset } from "@/api/assets";
import type { OwnedMediaAsset } from "@/api/contracts";
import { MediaCollectionNode, type MediaItemsUpdater } from "@/components/canvas/media-collection-node";
import { mediaItemLabel, moveMediaItem, safeMediaDisplayName } from "@/features/graph/media-collection";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";
import type { GraphMediaItem, GraphMediaType } from "@/features/graph/contracts";
import CanvasProjectPage from "@/pages/canvas/project";
import { useCanvasStore } from "@/stores/canvas/use-canvas-store";

const items: GraphMediaItem[] = [
    { id: "item-a", assetId: "asset-a", displayName: "一.png", mimeType: "image/png", bytes: 20 },
    { id: "item-b", assetId: "asset-b", displayName: "二.png", mimeType: "image/png", bytes: 30 },
    { id: "item-c", assetId: "asset-c", displayName: "三.png", mimeType: "image/png", bytes: 40 },
];

function collectionNode(mediaType: GraphMediaType = "image", collectionItems: GraphMediaItem[] = items): CanvasNodeData {
    const type = mediaType === "image" ? CanvasNodeType.Image : mediaType === "video" ? CanvasNodeType.Video : CanvasNodeType.Audio;
    return {
        id: `${mediaType}-collection`,
        type,
        title: mediaType === "image" ? "参考图片" : mediaType === "video" ? "参考视频" : "参考音频",
        position: { x: 10, y: 20 },
        width: 360,
        height: 260,
        metadata: {
            graph: { schemaVersion: 1, role: "media-collection", mediaType, outputPortId: "media", items: collectionItems },
        },
    };
}

afterEach(() => {
    cleanup();
    setCsrfToken(null);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    useCanvasStore.setState({ projects: [], projectSyncMetadata: {}, syncNotice: null, loadError: null, hydrated: true, projectsLoaded: true });
});

it("derives stable numbered labels from the persisted order", () => {
    expect(mediaItemLabel("image", 0)).toBe("@图片1");
    expect(mediaItemLabel("video", 14)).toBe("@视频15");
    expect(mediaItemLabel("audio", 2)).toBe("@音频3");
    expect(moveMediaItem(items, "item-c", -1).map((item) => item.assetId)).toEqual(["asset-a", "asset-c", "asset-b"]);
    expect(moveMediaItem(items, "missing", -1)).toBe(items);
    expect(safeMediaDisplayName("../../private\\frame\u0000.png", "image")).toBe("frame.png");
});

it("grows naturally through eight media items and only scrolls larger collections", () => {
    const expanded = Array.from({ length: 9 }, (_, index): GraphMediaItem => ({
        id: `item-${index}`,
        assetId: `asset-${index}`,
        displayName: `${index + 1}.png`,
        mimeType: "image/png",
        bytes: index + 1,
    }));
    const view = render(<MediaCollectionNode node={collectionNode("image", expanded.slice(0, 8))} onItemsChange={() => undefined} />);
    const list = view.container.querySelector("ol");
    expect(list).not.toHaveClass("max-h-80", "overflow-y-auto");
    expect(list).toHaveAttribute("data-overflowing", "false");

    view.rerender(<MediaCollectionNode node={collectionNode("image", expanded)} onItemsChange={() => undefined} />);
    expect(list).toHaveClass("max-h-80", "overflow-y-auto");
    expect(list).toHaveAttribute("data-overflowing", "true");
});

it("previews, removes, drags, and keyboard-reorders one ordered image collection", () => {
    const changes: GraphMediaItem[][] = [];
    let current = items;
    const change = (update: MediaItemsUpdater) => { current = update(current); changes.push(current); };
    const { rerender } = render(<MediaCollectionNode node={collectionNode()} onItemsChange={change} />);

    expect(screen.getByRole("img", { name: "@图片1 一.png" })).toHaveAttribute("src", "/api/v1/assets/asset-a/content");
    expect(screen.getByText("@图片2")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "上移 @图片3" }));
    expect(changes.at(-1)?.map((item) => item.assetId)).toEqual(["asset-a", "asset-c", "asset-b"]);

    rerender(<MediaCollectionNode node={collectionNode("image", current)} onItemsChange={change} />);
    fireEvent.dragStart(screen.getByTestId("media-item-item-a"));
    fireEvent.dragOver(screen.getByTestId("media-item-item-b"));
    fireEvent.drop(screen.getByTestId("media-item-item-b"));
    expect(changes.at(-1)?.map((item) => item.assetId)).toEqual(["asset-c", "asset-b", "asset-a"]);

    rerender(<MediaCollectionNode node={collectionNode("image", current)} onItemsChange={change} />);
    fireEvent.click(screen.getByRole("button", { name: "移除 @图片2" }));
    expect(changes.at(-1)?.map((item) => item.assetId)).toEqual(["asset-c", "asset-a"]);
});

it("uploads multiple files with progress, persists only active safe assets in selection order, and revokes previews", async () => {
    let finishFirst: (() => void) | undefined;
    let finishSecond: (() => void) | undefined;
    const upload = vi.fn((file: File, mediaType: GraphMediaType, onProgress: (percent: number) => void) => {
        onProgress(file.name === "first.png" ? 25 : 60);
        return new Promise<OwnedMediaAsset>((resolve) => {
            const finish = () => resolve({
                id: file.name === "first.png" ? "asset-first" : "asset-second",
                kind: "reference" as const,
                status: "active" as const,
                media_type: mediaType,
                mime_type: file.type,
                size_bytes: file.size,
                content_url: `/api/v1/assets/${file.name === "first.png" ? "asset-first" : "asset-second"}/content`,
            });
            if (file.name === "first.png") finishFirst = finish;
            else finishSecond = finish;
        });
    });
    const createObjectURL = vi.fn((file: File) => `blob:${file.name}`);
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
    const changes: GraphMediaItem[][] = [];
    let current: GraphMediaItem[] = [];
    render(<MediaCollectionNode node={collectionNode("image", [])} upload={upload} onItemsChange={(update) => { current = update(current); changes.push(current); }} />);

    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files: [
        new File(["a"], "first.png", { type: "image/png" }),
        new File(["bb"], "second.png", { type: "image/png" }),
    ] } });

    expect(await screen.findByText("first.png · 25%")).toBeVisible();
    expect(screen.getByText("second.png · 60%")).toBeVisible();
    finishSecond?.();
    finishFirst?.();
    await waitFor(() => expect(changes.at(-1)?.map((item) => item.assetId)).toEqual(["asset-first", "asset-second"]));
    expect(changes.at(-1)?.map((item) => item.displayName)).toEqual(["first.png", "second.png"]);
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledTimes(2));
});

it("keeps successful uploads, shows a safe error for failed files, and does not persist failures", async () => {
    const upload = vi.fn(async (file: File, mediaType: GraphMediaType) => {
        if (file.name === "bad.mp4") throw new Error("secret upstream stack");
        return { id: "asset-good", kind: "reference" as const, status: "active" as const, media_type: mediaType, mime_type: file.type, size_bytes: file.size, content_url: "/api/v1/assets/asset-good/content" };
    });
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:preview"), revokeObjectURL: vi.fn() });
    const changes: GraphMediaItem[][] = [];
    let current: GraphMediaItem[] = [];
    render(<MediaCollectionNode node={collectionNode("video", [])} upload={upload} onItemsChange={(update) => { current = update(current); changes.push(current); }} />);

    fireEvent.change(screen.getByLabelText("添加视频"), { target: { files: [
        new File(["good"], "good.mp4", { type: "video/mp4" }),
        new File(["bad"], "bad.mp4", { type: "video/mp4" }),
    ] } });

    expect(await screen.findByText("bad.mp4 上传失败，请重试。")).toBeVisible();
    expect(screen.queryByText(/secret upstream stack/)).not.toBeInTheDocument();
    expect(changes.at(-1)?.map((item) => item.assetId)).toEqual(["asset-good"]);
});

it("retries a failed file into its original ordered slot and releases its preview after success", async () => {
    const attempts = new Map<string, number>();
    const upload = vi.fn(async (file: File, mediaType: GraphMediaType) => {
        const attempt = (attempts.get(file.name) ?? 0) + 1;
        attempts.set(file.name, attempt);
        if (file.name === "first.png" && attempt === 1) throw new Error("temporary");
        return { id: `asset-${file.name}`, kind: "reference" as const, status: "active" as const, media_type: mediaType, mime_type: file.type, size_bytes: file.size, content_url: `/api/v1/assets/asset-${file.name}/content` };
    });
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL });
    let current: GraphMediaItem[] = [];
    render(<MediaCollectionNode node={collectionNode("image", [])} upload={upload} onItemsChange={(update) => { current = update(current); }} />);
    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files: [new File(["a"], "first.png", { type: "image/png" }), new File(["b"], "second.png", { type: "image/png" })] } });
    expect(await screen.findByText("first.png 上传失败，请重试。")).toBeVisible();
    expect(current.map((item) => item.assetId)).toEqual(["asset-second.png"]);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:second.png");
    expect(revokeObjectURL).not.toHaveBeenCalledWith("blob:first.png");

    fireEvent.click(screen.getByRole("button", { name: "重试 first.png" }));

    await waitFor(() => expect(current.map((item) => item.assetId)).toEqual(["asset-first.png", "asset-second.png"]));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:first.png");
});

it("removes failed uploads on demand and releases retained previews on unmount", async () => {
    const upload = vi.fn(async () => { throw new Error("failed"); });
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL });
    const view = render(<MediaCollectionNode node={collectionNode("video", [])} upload={upload} onItemsChange={() => undefined} />);
    fireEvent.change(screen.getByLabelText("添加视频"), { target: { files: [new File(["a"], "remove.mp4", { type: "video/mp4" })] } });
    expect(await screen.findByText("remove.mp4 上传失败，请重试。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "移除 remove.mp4" }));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:remove.mp4");

    fireEvent.change(screen.getByLabelText("添加视频"), { target: { files: [new File(["b"], "unmount-failed.mp4", { type: "video/mp4" })] } });
    expect(await screen.findByText("unmount-failed.mp4 上传失败，请重试。")).toBeVisible();
    view.unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:unmount-failed.mp4");
});

it("caps each collection at 30 active and pending items with a visible truncation message", async () => {
    const upload = vi.fn((_file: File, _mediaType: GraphMediaType, _progress: (percent: number) => void, signal: AbortSignal) => new Promise<OwnedMediaAsset>((_resolve, reject) => signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), { once: true })));
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL: vi.fn() });
    render(<MediaCollectionNode node={collectionNode("image", [])} upload={upload} onItemsChange={() => undefined} />);
    const files = Array.from({ length: 35 }, (_, index) => new File([String(index)], `limit-${index}.png`, { type: "image/png" }));

    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files } });

    expect(await screen.findByText("每个集合最多 30 个媒体文件，已忽略 5 个。")).toBeVisible();
    expect(screen.getAllByRole("status")).toHaveLength(30);
    expect(upload).toHaveBeenCalledTimes(3);
});

it("keeps previews available but removes all mutation controls in read-only mode", () => {
    render(<MediaCollectionNode node={collectionNode("audio", [{ ...items[0], mimeType: "audio/mpeg", displayName: "voice.mp3" }])} readOnly onItemsChange={() => { throw new Error("must not mutate"); }} />);

    expect(screen.getByLabelText("@音频1 voice.mp3")).toHaveAttribute("src", "/api/v1/assets/asset-a/content");
    expect(screen.queryByLabelText("添加音频")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /移除|上移|下移/ })).not.toBeInTheDocument();
    expect(screen.getByTestId("media-item-item-a")).not.toHaveAttribute("draggable", "true");
});

it("uploads through same-origin XHR with CSRF and reports bounded progress", async () => {
    class FakeXhr {
        static instance: FakeXhr;
        uploadListeners: Record<string, (event: ProgressEvent) => void> = {};
        listeners: Record<string, () => void> = {};
        headers: Record<string, string> = {};
        status = 201;
        responseText = "";
        withCredentials = false;
        method = "";
        url = "";
        upload = { addEventListener: (name: string, callback: (event: ProgressEvent) => void) => { this.uploadListeners[name] = callback; } };
        constructor() { FakeXhr.instance = this; }
        open(method: string, url: string) { this.method = method; this.url = url; }
        setRequestHeader(name: string, value: string) { this.headers[name] = value; }
        addEventListener(name: string, callback: () => void) { this.listeners[name] = callback; }
        send(body: FormData) {
            expect(body.get("media_type")).toBe("image");
            this.uploadListeners.progress(new ProgressEvent("progress", { lengthComputable: true, loaded: 1, total: 4 }));
            this.responseText = JSON.stringify({ asset_id: "asset-x", kind: "reference", status: "active", media_type: "image", mime_type: "image/png", size_bytes: 4, created_at: "2026-08-11T00:00:00Z", content_url: "/api/v1/assets/asset-x/content" });
            this.listeners.load();
        }
    }
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    setCsrfToken("csrf-123");
    const progress: number[] = [];

    const result = await uploadMediaAsset(new File(["data"], "frame.png", { type: "image/png" }), "image", (percent) => progress.push(percent));

    expect(FakeXhr.instance.method).toBe("POST");
    expect(FakeXhr.instance.url).toBe("/api/v1/assets");
    expect(FakeXhr.instance.withCredentials).toBe(true);
    expect(FakeXhr.instance.headers["X-CSRF-Token"]).toBe("csrf-123");
    expect(progress).toEqual([25, 100]);
    expect(result).toMatchObject({ id: "asset-x", media_type: "image", content_url: "/api/v1/assets/asset-x/content" });
});

it("aborts same-origin XHR with an AbortError when its signal is cancelled", async () => {
    class AbortableXhr {
        static instance: AbortableXhr;
        listeners: Record<string, () => void> = {};
        upload = { addEventListener: () => undefined };
        withCredentials = false;
        status = 0;
        responseText = "";
        constructor() { AbortableXhr.instance = this; }
        open() { return undefined; }
        setRequestHeader() { return undefined; }
        addEventListener(name: string, callback: () => void) { this.listeners[name] = callback; }
        send() { return undefined; }
        abort() { this.listeners.abort?.(); }
    }
    vi.stubGlobal("XMLHttpRequest", AbortableXhr);
    const controller = new AbortController();
    const request = uploadMediaAsset(new File(["data"], "frame.png", { type: "image/png" }), "image", undefined, controller.signal);

    controller.abort();

    await expect(request).rejects.toMatchObject({ name: "AbortError" });
});

it("best-effort deletes an owned asset through same-origin CSRF protection", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetch);
    setCsrfToken("csrf-delete");

    await deleteMediaAsset("asset-cleanup");

    const [path, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/assets/asset-cleanup");
    expect(init.method).toBe("DELETE");
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf-delete");
});

it("serializes overlapping batches and applies upload results to the latest reordered collection", async () => {
    const calls: string[] = [];
    const resolvers = new Map<string, (asset: OwnedMediaAsset) => void>();
    const upload = vi.fn((file: File, mediaType: GraphMediaType, _progress: (percent: number) => void, signal: AbortSignal) => {
        calls.push(file.name);
        return new Promise<OwnedMediaAsset>((resolve, reject) => {
            resolvers.set(file.name, resolve);
            signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), { once: true });
        });
    });
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL: vi.fn() });
    let current = items;
    render(<MediaCollectionNode node={collectionNode("image", items)} upload={upload} onItemsChange={(update) => { current = update(current); }} />);
    const input = screen.getByLabelText("添加图片");

    fireEvent.change(input, { target: { files: [new File(["a"], "batch-a.png", { type: "image/png" })] } });
    fireEvent.change(input, { target: { files: [new File(["b"], "batch-b.png", { type: "image/png" })] } });
    await waitFor(() => expect(calls).toEqual(["batch-a.png"]));
    fireEvent.click(screen.getByRole("button", { name: "上移 @图片3" }));
    fireEvent.click(screen.getByRole("button", { name: "移除 @图片1" }));
    resolvers.get("batch-a.png")?.({ id: "asset-batch-a", kind: "reference", status: "active", media_type: "image", mime_type: "image/png", size_bytes: 1, content_url: "/api/v1/assets/asset-batch-a/content" });
    await waitFor(() => expect(calls).toEqual(["batch-a.png", "batch-b.png"]));
    resolvers.get("batch-b.png")?.({ id: "asset-batch-b", kind: "reference", status: "active", media_type: "image", mime_type: "image/png", size_bytes: 1, content_url: "/api/v1/assets/asset-batch-b/content" });

    await waitFor(() => expect(current.map((item) => item.assetId)).toEqual(["asset-c", "asset-b", "asset-batch-a", "asset-batch-b"]));
    expect(new Set(current.map((item) => item.assetId)).size).toBe(current.length);
});

it("limits one 30-item batch to three active uploads and preserves selection order under reverse cohort completion", async () => {
    let active = 0;
    let maxActive = 0;
    const completed: string[] = [];
    const resolvers = new Map<number, () => void>();
    const upload = vi.fn((file: File, mediaType: GraphMediaType) => {
        const index = Number(file.name.slice(5, 7));
        active += 1;
        maxActive = Math.max(maxActive, active);
        return new Promise<OwnedMediaAsset>((resolve) => resolvers.set(index, () => {
            active -= 1;
            completed.push(file.name);
            resolve({ id: `asset-${index}`, kind: "reference", status: "active", media_type: mediaType, mime_type: file.type, size_bytes: file.size, content_url: `/api/v1/assets/asset-${index}/content` });
        }));
    });
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL: vi.fn() });
    let current: GraphMediaItem[] = [];
    render(<MediaCollectionNode node={collectionNode("image", [])} upload={upload} onItemsChange={(update) => { current = update(current); }} />);
    const files = Array.from({ length: 30 }, (_, index) => new File([String(index)], `item-${String(index + 1).padStart(2, "0")}.png`, { type: "image/png" }));

    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files } });

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(3));
    let completedCount = 0;
    for (let cohort = 1; cohort <= 30; cohort += 3) {
        for (let index = cohort + 2; index >= cohort; index -= 1) {
            resolvers.get(index)?.();
            completedCount += 1;
            await waitFor(() => expect(upload).toHaveBeenCalledTimes(Math.min(30, 3 + completedCount)));
        }
    }
    await waitFor(() => expect(current).toHaveLength(30), { timeout: 5000 });
    expect(maxActive).toBeLessThanOrEqual(3);
    expect(current.map((item) => item.assetId)).toEqual(Array.from({ length: 30 }, (_, index) => `asset-${index + 1}`));
    expect(completed.slice(0, 3)).toEqual(["item-03.png", "item-02.png", "item-01.png"]);
});

it("shares the three-upload scheduler across multiple collection nodes", async () => {
    let active = 0;
    let maxActive = 0;
    const upload = vi.fn(async (file: File, mediaType: GraphMediaType) => {
        active += 1;
        maxActive = Math.max(maxActive, active);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active -= 1;
        return { id: `asset-${file.name}`, kind: "reference" as const, status: "active" as const, media_type: mediaType, mime_type: file.type, size_bytes: file.size, content_url: `/api/v1/assets/asset-${file.name}/content` };
    });
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL: vi.fn() });
    let first: GraphMediaItem[] = [];
    let second: GraphMediaItem[] = [];
    const secondNode = { ...collectionNode("image", []), id: "second-image-collection", title: "第二组图片" };
    render(<><MediaCollectionNode node={collectionNode("image", [])} upload={upload} onItemsChange={(update) => { first = update(first); }} /><MediaCollectionNode node={secondNode} upload={upload} onItemsChange={(update) => { second = update(second); }} /></>);
    const inputs = screen.getAllByLabelText("添加图片");
    fireEvent.change(inputs[0], { target: { files: Array.from({ length: 6 }, (_, index) => new File(["a"], `a-${index}.png`, { type: "image/png" })) } });
    fireEvent.change(inputs[1], { target: { files: Array.from({ length: 6 }, (_, index) => new File(["b"], `b-${index}.png`, { type: "image/png" })) } });

    await waitFor(() => expect(first.length + second.length).toBe(12), { timeout: 5000 });
    expect(maxActive).toBeLessThanOrEqual(3);
});

it("releases shared scheduler slots after synchronous upload failures", async () => {
    let calls = 0;
    const upload = vi.fn((file: File, mediaType: GraphMediaType): Promise<OwnedMediaAsset> => {
        calls += 1;
        if (calls <= 3) throw new Error("synchronous upload failure");
        return Promise.resolve({
            id: "asset-recovered",
            kind: "reference",
            status: "active",
            media_type: mediaType,
            mime_type: file.type,
            size_bytes: file.size,
            content_url: "/api/v1/assets/asset-recovered/content",
        });
    });
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL: vi.fn() });
    let current: GraphMediaItem[] = [];
    render(<MediaCollectionNode node={collectionNode("image", [])} upload={upload} onItemsChange={(update) => { current = update(current); }} />);

    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files: Array.from(
        { length: 4 },
        (_, index) => new File([String(index)], `sync-${index}.png`, { type: "image/png" }),
    ) } });

    await waitFor(() => expect(upload).toHaveBeenCalledTimes(4));
    await waitFor(() => expect(current.map((item) => item.assetId)).toEqual(["asset-recovered"]));
    expect(screen.getAllByText(/上传失败，请重试/)).toHaveLength(3);
});

it("never starts a queued cancelled upload and releases repeated cancellation state", async () => {
    const upload = vi.fn((_file: File, _mediaType: GraphMediaType, _progress: (percent: number) => void, signal: AbortSignal) => new Promise<OwnedMediaAsset>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), { once: true });
    }));
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL: vi.fn() });
    render(<MediaCollectionNode node={collectionNode("video", [])} upload={upload} onItemsChange={() => undefined} />);
    const input = screen.getByLabelText("添加视频");

    for (let cycle = 0; cycle < 5; cycle += 1) {
        const files = Array.from({ length: 4 }, (_, index) => new File([String(index)], `cycle-${cycle}-${index}.mp4`, { type: "video/mp4" }));
        fireEvent.change(input, { target: { files } });
        await waitFor(() => expect(upload).toHaveBeenCalledTimes((cycle + 1) * 3));
        for (const file of files) fireEvent.click(screen.getByRole("button", { name: `取消上传 ${file.name}` }));
        await waitFor(() => expect(screen.queryByText(new RegExp(`cycle-${cycle}-`))).not.toBeInTheDocument());
    }

    expect(upload).toHaveBeenCalledTimes(15);
    expect(upload.mock.calls.some(([file]) => (file as File).name.endsWith("-3.mp4"))).toBe(false);
    expect(screen.queryAllByRole("status")).toHaveLength(0);
});

it("aborts only the three active workers and clears the remaining queue on unmount", async () => {
    const signals: AbortSignal[] = [];
    const upload = vi.fn((_file: File, _mediaType: GraphMediaType, _progress: (percent: number) => void, signal: AbortSignal) => {
        signals.push(signal);
        return new Promise<OwnedMediaAsset>((_resolve, reject) => signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), { once: true }));
    });
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL: vi.fn() });
    const view = render(<MediaCollectionNode node={collectionNode("audio", [])} upload={upload} onItemsChange={() => undefined} />);
    const files = Array.from({ length: 8 }, (_, index) => new File([String(index)], `queued-${index}.mp3`, { type: "audio/mpeg" }));
    fireEvent.change(screen.getByLabelText("添加音频"), { target: { files } });
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(3));

    view.unmount();
    await waitFor(() => expect(signals.every((signal) => signal.aborted)).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(upload).toHaveBeenCalledTimes(3);
});

it("cancels an in-flight item immediately and aborts all remaining uploads on unmount", async () => {
    const signals: AbortSignal[] = [];
    const upload = vi.fn((_file: File, _mediaType: GraphMediaType, _progress: (percent: number) => void, signal: AbortSignal) => {
        signals.push(signal);
        return new Promise<OwnedMediaAsset>((_resolve, reject) => signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), { once: true }));
    });
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL });
    let current: GraphMediaItem[] = [];
    const view = render(<MediaCollectionNode node={collectionNode("video", [])} upload={upload} onItemsChange={(update) => { current = update(current); }} />);
    const input = screen.getByLabelText("添加视频");
    fireEvent.change(input, { target: { files: [new File(["a"], "cancel.mp4", { type: "video/mp4" })] } });
    await waitFor(() => expect(signals).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "取消上传 cancel.mp4" }));
    expect(signals[0].aborted).toBe(true);
    await waitFor(() => expect(screen.queryByText(/cancel\.mp4/)).not.toBeInTheDocument());

    fireEvent.change(input, { target: { files: [new File(["b"], "unmount.mp4", { type: "video/mp4" })] } });
    await waitFor(() => expect(signals).toHaveLength(2));
    view.unmount();
    expect(signals[1].aborted).toBe(true);
    expect(current).toEqual([]);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:cancel.mp4");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:unmount.mp4");
});

it("aborts active uploads when the node becomes read-only or its identity changes", async () => {
    const signals: AbortSignal[] = [];
    const upload = vi.fn((_file: File, _mediaType: GraphMediaType, _progress: (percent: number) => void, signal: AbortSignal) => {
        signals.push(signal);
        return new Promise<OwnedMediaAsset>((_resolve, reject) => signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), { once: true }));
    });
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL: vi.fn() });
    const change = vi.fn();
    const first = collectionNode("image", []);
    const { rerender } = render(<MediaCollectionNode node={first} upload={upload} onItemsChange={change} />);

    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files: [new File(["a"], "readonly.png", { type: "image/png" })] } });
    await waitFor(() => expect(signals).toHaveLength(1));
    rerender(<MediaCollectionNode node={first} readOnly upload={upload} onItemsChange={change} />);
    expect(signals[0].aborted).toBe(true);
    await waitFor(() => expect(screen.queryByText(/readonly\.png/)).not.toBeInTheDocument());

    rerender(<MediaCollectionNode node={first} upload={upload} onItemsChange={change} />);
    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files: [new File(["b"], "changed.png", { type: "image/png" })] } });
    await waitFor(() => expect(signals).toHaveLength(2));
    rerender(<MediaCollectionNode node={{ ...first, id: "different-node" }} upload={upload} onItemsChange={change} />);
    expect(signals[1].aborted).toBe(true);
    await waitFor(() => expect(screen.queryByText(/changed\.png/)).not.toBeInTheDocument());
    expect(change).not.toHaveBeenCalled();
});

it("never writes an old upload through callbacks from a newly rendered node scope", async () => {
    let finish: ((asset: OwnedMediaAsset) => void) | undefined;
    const upload = vi.fn(() => new Promise<OwnedMediaAsset>((resolve) => { finish = resolve; }));
    const removeAsset = vi.fn().mockResolvedValue(undefined);
    const oldChange = vi.fn();
    const newChange = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:scoped"), revokeObjectURL: vi.fn() });
    const first = collectionNode("image", []);
    const view = render(<MediaCollectionNode node={first} upload={upload} removeAsset={removeAsset} onItemsChange={oldChange} />);
    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files: [new File(["a"], "scoped.png", { type: "image/png" })] } });
    await waitFor(() => expect(upload).toHaveBeenCalledOnce());
    view.rerender(<MediaCollectionNode node={{ ...first, id: "new-scope" }} upload={upload} removeAsset={removeAsset} onItemsChange={newChange} />);

    finish?.({ id: "asset-scoped", kind: "reference", status: "active", media_type: "image", mime_type: "image/png", size_bytes: 1, content_url: "/api/v1/assets/asset-scoped/content" });

    await waitFor(() => expect(removeAsset).toHaveBeenCalledWith("asset-scoped"));
    expect(oldChange).not.toHaveBeenCalled();
    expect(newChange).not.toHaveBeenCalled();
});

it("deletes a late successful upload when unmounted or when project writeback declines it", async () => {
    let finish: ((asset: OwnedMediaAsset) => void) | undefined;
    const asset: OwnedMediaAsset = { id: "asset-orphan", kind: "reference", status: "active", media_type: "image", mime_type: "image/png", size_bytes: 1, content_url: "/api/v1/assets/asset-orphan/content" };
    const upload = vi.fn(() => new Promise<OwnedMediaAsset>((resolve) => { finish = resolve; }));
    const removeAsset = vi.fn().mockRejectedValue(new Error("network unavailable"));
    const change = vi.fn();
    const view = render(<MediaCollectionNode node={collectionNode("image", [])} upload={upload} removeAsset={removeAsset} onItemsChange={change} />);
    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files: [new File(["a"], "late.png", { type: "image/png" })] } });
    await waitFor(() => expect(upload).toHaveBeenCalledOnce());
    view.unmount();
    finish?.(asset);
    await waitFor(() => expect(removeAsset).toHaveBeenCalledWith("asset-orphan"));
    expect(change).not.toHaveBeenCalled();

    removeAsset.mockClear();
    render(<MediaCollectionNode node={collectionNode("image", [])} upload={vi.fn().mockResolvedValue(asset)} removeAsset={removeAsset} onItemsChange={() => false} />);
    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files: [new File(["b"], "declined.png", { type: "image/png" })] } });
    await waitFor(() => expect(removeAsset).toHaveBeenCalledWith("asset-orphan"));
});

it("normalizes and persists inline display-name edits while read-only mode hides the editor", () => {
    let current = items;
    const { rerender } = render(<MediaCollectionNode node={collectionNode()} onItemsChange={(update) => { current = update(current); }} />);
    const editor = screen.getByLabelText("重命名 @图片1");
    fireEvent.change(editor, { target: { value: "../renamed\u0000.png" } });
    fireEvent.blur(editor);
    expect(current[0].displayName).toBe("renamed.png");

    rerender(<MediaCollectionNode node={collectionNode("image", current)} readOnly onItemsChange={() => { throw new Error("must not edit"); }} />);
    expect(screen.queryByLabelText("重命名 @图片1")).not.toBeInTheDocument();
    expect(screen.getByText("renamed.png")).toBeVisible();
});

it("normalizes safe server asset metadata for portrait polling without exposing storage fields", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
        asset_id: "portrait-x", kind: "portrait", status: "processing", media_type: "image", mime_type: "image/png",
        size_bytes: 42, created_at: "2026-08-11T00:00:00Z", content_url: "/api/v1/assets/portrait-x/content", relative_path: "must-not-pass",
    }), { headers: { "content-type": "application/json" } })));

    const result = await fetchAsset("portrait-x");

    expect(result).toEqual({ id: "portrait-x", kind: "portrait", status: "processing", media_type: "image", mime_type: "image/png", size_bytes: 42, content_url: "/api/v1/assets/portrait-x/content" });
    expect(result).not.toHaveProperty("relative_path");
});

it("creates all three collection nodes and persists uploaded asset order in the canvas project", async () => {
    let uploadNumber = 0;
    class FakeXhr {
        status = 201;
        responseText = "";
        withCredentials = false;
        upload = { addEventListener: () => undefined };
        listeners: Record<string, () => void> = {};
        open() { return undefined; }
        setRequestHeader() { return undefined; }
        addEventListener(name: string, callback: () => void) { this.listeners[name] = callback; }
        send(body: FormData) {
            uploadNumber += 1;
            const file = body.get("file") as File;
            const assetId = `asset-${uploadNumber}`;
            this.responseText = JSON.stringify({ asset_id: assetId, kind: "reference", status: "active", media_type: "image", mime_type: file.type, size_bytes: file.size, content_url: `/api/v1/assets/${assetId}/content` });
            this.listeners.load();
        }
    }
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL: vi.fn() });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ models: [] }), { headers: { "content-type": "application/json" } })));
    const projectId = useCanvasStore.getState().createProject("Media Canvas");
    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);

    fireEvent.click(screen.getByRole("button", { name: "参考图节点" }));
    fireEvent.change(screen.getByLabelText("添加图片"), { target: { files: [
        new File(["a"], "first.png", { type: "image/png" }),
        new File(["bb"], "second.png", { type: "image/png" }),
    ] } });
    await waitFor(() => {
        const imageCollection = useCanvasStore.getState().openProject(projectId)?.nodes.find((node) => node.metadata?.graph?.role === "media-collection" && node.metadata.graph.mediaType === "image");
        expect(imageCollection?.metadata?.graph?.role === "media-collection" ? imageCollection.metadata.graph.items.map((item) => item.assetId) : []).toEqual(["asset-1", "asset-2"]);
    });
    fireEvent.click(screen.getByRole("button", { name: "上移 @图片2" }));
    await waitFor(() => {
        const imageCollection = useCanvasStore.getState().openProject(projectId)?.nodes.find((node) => node.metadata?.graph?.role === "media-collection" && node.metadata.graph.mediaType === "image");
        expect(imageCollection?.metadata?.graph?.role === "media-collection" ? imageCollection.metadata.graph.items.map((item) => item.assetId) : []).toEqual(["asset-2", "asset-1"]);
    });

    fireEvent.click(screen.getByRole("button", { name: "参考视频节点" }));
    fireEvent.click(screen.getByRole("button", { name: "参考音频节点" }));
    const mediaTypes = useCanvasStore.getState().openProject(projectId)?.nodes.flatMap((node) => node.metadata?.graph?.role === "media-collection" ? [node.metadata.graph.mediaType] : []);
    expect(mediaTypes).toEqual(["image", "video", "audio"]);
});
