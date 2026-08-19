import { page } from "vitest/browser";
import { createRoot, type Root } from "react-dom/client";
import { flushSync } from "react-dom";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ProductShell } from "@/components/layout/product-shell";
import CanvasProjectPage from "@/pages/canvas/project";
import AdminModelsPage from "@/pages/admin/models";
import { appendJobResults } from "@/features/generation/result-node";
import { clearCanvasInMemory, useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { useSessionStore } from "@/stores/portal/use-session-store";
import { clearStorageScope, setStorageScope } from "@/storage/scope";
import "@/styles/globals.css";

type Bounds = Pick<DOMRect, "bottom" | "left" | "right" | "top">;

let root: Root;

const offlineResultUrl = "/api/v1/results/fixture-result-job/0";
const offlineResultPng = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAATUlEQVR42u3PQQkAAAgEsCtp/yheBN/CYAWW7PwmICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHAp64BBPFtd4VMAAAAASUVORK5CYII=";

function pngBytes(encoded: string) {
    return Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
}

function bounds(selector: string): Bounds {
    const element = document.querySelector(selector);
    expect(element, `missing layout element: ${selector}`).not.toBeNull();
    return element!.getBoundingClientRect();
}

function overlapArea(first: Bounds, second: Bounds) {
    return Math.max(0, Math.min(first.right, second.right) - Math.max(first.left, second.left)) * Math.max(0, Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top));
}

function chooseFile(input: HTMLInputElement, files: File[]) {
    Object.defineProperty(input, "files", { configurable: true, value: files });
    input.dispatchEvent(new Event("change", { bubbles: true }));
}

beforeEach(async () => {
    await setStorageScope({ environment: "test", userId: "responsive-user" });
    clearCanvasInMemory();
    useCanvasStore.setState({ hydrated: true, projectsLoaded: true });
    useSessionStore.setState({
        session: { user_id: "responsive-user", username: "响应式验收", role: "user", must_change_password: false },
        environment: "test",
        loading: false,
        errorCode: null,
    });
    vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
            const path = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
            if (path === offlineResultUrl) return new Response(pngBytes(offlineResultPng), { headers: { "content-type": "image/png" } });
            return new Response(JSON.stringify({ models: [] }), { headers: { "content-type": "application/json" } });
        }),
    );
    document.body.innerHTML = '<div id="responsive-test-root"></div>';
    root = createRoot(document.getElementById("responsive-test-root")!);
});

afterEach(() => {
    flushSync(() => root.unmount());
    vi.unstubAllGlobals();
    clearCanvasInMemory();
    clearStorageScope();
    document.body.replaceChildren();
});

it.each([415, 240])("keeps canvas controls contained and non-overlapping at %i px", async (viewportWidth) => {
    await page.viewport(viewportWidth, 900);
    const projectId = useCanvasStore.getState().createProject("Responsive canvas");
    flushSync(() =>
        root.render(
            <MemoryRouter initialEntries={[`/canvas/${projectId}`]}>
                <ProductShell>
                    <Routes>
                        <Route path="/canvas/:id" element={<CanvasProjectPage />} />
                    </Routes>
                </ProductShell>
            </MemoryRouter>,
        ),
    );
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

    const canvas = bounds('[data-testid="studio-canvas"]');
    const controls = bounds('[data-testid="studio-canvas"] [data-canvas-no-zoom]');
    const palette = bounds('[data-testid="studio-palette"]');
    const tray = bounds('[data-testid="task-tray"]');

    expect(controls.left).toBeGreaterThanOrEqual(canvas.left);
    expect(controls.right).toBeLessThanOrEqual(canvas.right);
    expect(controls.top).toBeGreaterThanOrEqual(canvas.top);
    expect(controls.bottom).toBeLessThanOrEqual(canvas.bottom);
    expect(overlapArea(controls, tray)).toBe(0);
    expect(overlapArea(palette, tray)).toBe(0);
    expect(palette.left).toBeGreaterThanOrEqual(0);
    expect(palette.right).toBeLessThanOrEqual(window.innerWidth);
    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(window.innerWidth);

    const controlClasses = document.querySelector('[data-testid="studio-canvas"] [data-canvas-no-zoom]')!.classList;
    expect(controlClasses).toContain("left-4");
    expect(controlClasses).toContain("max-w-[calc(100%-2rem)]");
});

it("keeps the logical-model administrator usable without horizontal overflow at 415 px", async () => {
    await page.viewport(415, 900);
    useSessionStore.setState({ session: { user_id: "admin", username: "管理员", role: "admin", must_change_password: false } });
    const contract = { operation: "image.edit", input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }, { port_id: "reference_images", media_type: "image", min_items: 1, max_items: 10 }], output_media_type: "image", parameter_schema: { type: "object", "x-aicc-profile": "banana", properties: { aspect_ratio: { type: "string", enum: ["1:1", "16:9", "9:16", "4:3", "3:4"], default: "1:1", title: "画面比例" }, image_size: { type: "string", enum: ["1K", "2K", "4K"], default: "2K", title: "图片尺寸" } }, required: ["aspect_ratio", "image_size"], additionalProperties: false }, parameter_mappings: { aspect_ratio: "aspectRatio", image_size: "imageSize" } };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        let body: unknown = {};
        if (url.includes("/admin/users")) body = { users: [{ user_id: "user", username: "user", display_name: "普通用户", role: "user", enabled: true, must_change_password: false, model_ids: ["banana"], created_at: 1, updated_at: 1 }] };
        else if (url.includes("/admin/models")) body = { models: [{ model_id: "banana", service_id: "banana", display_name: "Nano Banana", operations: ["image.edit"], input_media: ["text", "image"], parameter_schema: {} }] };
        else if (url.includes("/credential-pools")) body = { pools: [{ pool_id: "banana-chiyun", provider_id: "chiyun-banana", adapter_type: "chiyun_gemini_images", group: "banana", allowed_families: ["nano-banana"], revision_digest: "a".repeat(64), key_count: 2, total_capacity: 4, capacity_status: "available", available_count: 2, busy_count: 0, circuit_status: "unsupported", circuit_open_count: null }] };
        else if (url.includes("/routes")) body = { routes: [] };
        else if (url.includes("/logical-models")) body = { models: [{ model_id: "banana", display_name: "Nano Banana", introduction: "多参考图编辑", modality: "image", operation_contracts: [contract], enabled: true, archived_at: null, revision: 1 }] };
        return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
    }));
    flushSync(() => root.render(<MemoryRouter><ProductShell><AdminModelsPage /></ProductShell></MemoryRouter>));
    await expect.element(page.getByRole("heading", { name: "模型与调用线路" })).toBeVisible();
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(window.innerWidth);
    await expect.element(page.getByRole("heading", { name: "调用设置" })).toBeVisible();
    await expect.element(page.getByRole("article", { name: "Chiyun 调用设置" })).toBeVisible();
    const requiredControls = [
        page.getByLabelText("启用 Chiyun"),
        page.getByLabelText("Chiyun 凭据池"),
        page.getByLabelText("Chiyun 优先级"),
        page.getByLabelText("Chiyun 最大并发"),
        page.getByRole("button", { name: "保存 Chiyun 设置" }),
        page.getByLabelText("选择账号"),
        page.getByRole("button", { name: "保存派发" }),
    ];
    for (const control of requiredControls) {
        await expect.element(control).toBeVisible();
        const rectangle = (await control.element()).getBoundingClientRect();
        expect(rectangle.left).toBeGreaterThanOrEqual(0);
        expect(rectangle.right).toBeLessThanOrEqual(window.innerWidth);
    }
    await page.getByLabelText("Chiyun 凭据池").selectOptions("banana-chiyun");
    await expect.element(page.getByRole("button", { name: "保存 Chiyun 设置" })).not.toBeDisabled();
});

it("runs the connected media graph editing path in desktop Chromium", async () => {
    await page.viewport(1280, 900);
    const models = [
        {
            model_id: "seedream-fixture",
            service_id: "ark-image",
            display_name: "Seedream Fixture",
            operations: ["image.generate", "image.edit"],
            input_media: ["text", "image"],
            input_ports: [
                { port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 },
                { port_id: "reference_images", media_type: "image", min_items: 0, max_items: 14 },
            ],
            parameter_mappings: { size: "size", count: "n" },
            parameter_schema: { type: "object", properties: { size: { type: "string", enum: ["1024x1024", "2048x2048"], default: "1024x1024" }, count: { type: "integer", minimum: 1, maximum: 4, default: 1 } }, additionalProperties: false },
        },
        {
            model_id: "seedance-fixture",
            service_id: "ark-video",
            display_name: "Seedance Fixture",
            operations: ["video.generate"],
            input_media: ["text", "image", "audio"],
            input_ports: [
                { port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 },
                { port_id: "reference_images", media_type: "image", min_items: 0, max_items: 9 },
                { port_id: "reference_audio", media_type: "audio", min_items: 0, max_items: 3 },
            ],
            parameter_mappings: { ratio: "ratio", duration: "duration" },
            parameter_schema: { type: "object", properties: { ratio: { type: "string", enum: ["16:9", "9:16"], default: "16:9" }, duration: { type: "integer", minimum: 4, maximum: 15, default: 5 } }, additionalProperties: false },
        },
    ];
    vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
            const path = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
            if (path === offlineResultUrl) return new Response(pngBytes(offlineResultPng), { headers: { "content-type": "image/png" } });
            return new Response(JSON.stringify({ models }), { headers: { "content-type": "application/json" } });
        }),
    );
    let assetSequence = 0;
    class FixtureUpload extends EventTarget {
        upload = new EventTarget();
        status = 0;
        responseText = "";
        withCredentials = false;
        open() {}
        setRequestHeader() {}
        send(body: FormData) {
            const mediaType = String(body.get("media_type"));
            const file = body.get("file") as File;
            assetSequence += 1;
            this.status = 201;
            this.responseText = JSON.stringify({ asset_id: `fixture-${assetSequence}`, kind: "reference", status: "active", media_type: mediaType, mime_type: file.type, size_bytes: file.size });
            queueMicrotask(() => this.dispatchEvent(new Event("load")));
        }
        abort() {
            this.dispatchEvent(new Event("abort"));
        }
    }
    vi.stubGlobal("XMLHttpRequest", FixtureUpload as unknown as typeof XMLHttpRequest);
    const projectId = useCanvasStore.getState().createProject("Connected graph");
    flushSync(() =>
        root.render(
            <MemoryRouter initialEntries={[`/canvas/${projectId}`]}>
                <ProductShell>
                    <Routes>
                        <Route path="/canvas/:id" element={<CanvasProjectPage />} />
                    </Routes>
                </ProductShell>
            </MemoryRouter>,
        ),
    );

    const panSurface = document.querySelector<HTMLElement>('[data-testid="infinite-canvas"]')!;
    panSurface.setPointerCapture = () => undefined;
    panSurface.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0, pointerId: 91, clientX: 80, clientY: 90 }));
    window.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, pointerId: 91, clientX: 150, clientY: 160 }));
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    window.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, pointerId: 91, clientX: 310, clientY: 330 }));
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    window.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, pointerId: 91, clientX: 310, clientY: 330 }));
    expect(useCanvasStore.getState().openProject(projectId)!.viewport).toMatchObject({ x: 230, y: 240 });
    useCanvasStore.getState().updateProject(projectId, { viewport: { x: 0, y: 0, k: 1 } });
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

    await page.getByRole("button", { name: "提示词节点" }).click();
    expect(
        useCanvasStore
            .getState()
            .openProject(projectId)!
            .nodes.filter((node) => node.metadata?.graph?.role === "prompt"),
    ).toHaveLength(1);
    await expect.element(page.getByRole("button", { name: "提示词节点" })).not.toBeDisabled();
    await page.getByLabelText("提示词内容").fill("手动提示词");
    chooseFile(document.querySelector('input[aria-label="导入 TXT"]')!, [new File(["来自 TXT 的提示词"], "prompt.txt", { type: "text/plain" })]);
    await expect.element(page.getByLabelText("提示词内容")).toHaveValue("来自 TXT 的提示词");
    await page.getByRole("button", { name: "提示词节点" }).click();
    expect(
        useCanvasStore
            .getState()
            .openProject(projectId)!
            .nodes.filter((node) => node.metadata?.graph?.role === "prompt"),
    ).toHaveLength(2);

    const canvasElement = document.querySelector<HTMLElement>('[data-testid="infinite-canvas"]')!;
    const canvasBounds = canvasElement.getBoundingClientRect();
    canvasElement.dispatchEvent(new MouseEvent("contextmenu", {
        bubbles: true,
        cancelable: true,
        clientX: canvasBounds.left + 420,
        clientY: canvasBounds.top + 260,
    }));
    await expect.element(page.getByRole("menu", { name: "创建节点" })).toBeVisible();
    await page.getByRole("menuitem", { name: "参考图片" }).click();
    const contextCreatedImage = useCanvasStore
        .getState()
        .openProject(projectId)!
        .nodes.find((node) => node.metadata?.graph?.role === "media-collection" && node.metadata.graph.mediaType === "image")!;
    expect(contextCreatedImage.position.x).toBeCloseTo(420, 0);
    expect(contextCreatedImage.position.y).toBeCloseTo(260, 0);
    await expect.element(page.getByTestId(`draggable-node-${contextCreatedImage.id}`)).toHaveAttribute("aria-selected", "true");

    window.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "c", ctrlKey: true }));
    window.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "v", ctrlKey: true }));
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    const imageCollectionsAfterPaste = useCanvasStore
        .getState()
        .openProject(projectId)!
        .nodes.filter((node) => node.metadata?.graph?.role === "media-collection" && node.metadata.graph.mediaType === "image");
    expect(imageCollectionsAfterPaste).toHaveLength(2);
    expect(imageCollectionsAfterPaste[1].position).toEqual({ x: contextCreatedImage.position.x + 32, y: contextCreatedImage.position.y + 32 });
    window.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "F2" }));
    await expect.element(page.getByRole("dialog", { name: "重命名节点" })).toBeVisible();
    await page.getByRole("textbox", { name: "节点名称" }).fill("备用参考图");
    await page.getByRole("button", { name: "保存名称" }).click();
    expect(useCanvasStore.getState().openProject(projectId)!.nodes.find((node) => node.id === imageCollectionsAfterPaste[1].id)?.title).toBe("备用参考图");
    window.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Delete" }));
    expect(useCanvasStore.getState().openProject(projectId)!.nodes.some((node) => node.id === imageCollectionsAfterPaste[1].id)).toBe(false);

    await page.getByRole("button", { name: "参考视频节点" }).click();
    await page.getByRole("button", { name: "参考音频节点" }).click();
    chooseFile(document.querySelector('input[aria-label="添加图片"]')!, [new File(["one"], "one.png", { type: "image/png" }), new File(["two"], "two.png", { type: "image/png" })]);
    chooseFile(document.querySelector('input[aria-label="添加视频"]')!, [new File(["video"], "clip.mp4", { type: "video/mp4" })]);
    chooseFile(document.querySelector('input[aria-label="添加音频"]')!, [new File(["audio"], "voice.wav", { type: "audio/wav" })]);
    await expect.element(page.getByText("@图片1")).toBeVisible();
    await expect.element(page.getByText("@图片2")).toBeVisible();
    await expect.element(page.getByText("@视频1")).toBeVisible();
    await expect.element(page.getByText("@音频1")).toBeVisible();
    {
        const current = useCanvasStore.getState().openProject(projectId)!;
        const positions = { prompt: { x: 0, y: 0 }, image: { x: 340, y: 0 }, video: { x: 0, y: 340 }, audio: { x: 430, y: 340 } } as const;
        let promptIndex = 0;
        useCanvasStore.getState().updateProject(projectId, {
            nodes: current.nodes.map((node) => {
                const graph = node.metadata?.graph;
                if (graph?.role === "prompt") return { ...node, position: { x: 0, y: promptIndex++ * 680 } };
                const key = graph?.role === "media-collection" ? graph.mediaType : null;
                return key ? { ...node, position: positions[key] } : node;
            }),
        });
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    }
    await page.getByRole("button", { name: "下移 @图片1" }).click();
    expect(
        useCanvasStore
            .getState()
            .openProject(projectId)!
            .nodes.find((node) => node.metadata?.graph?.role === "media-collection" && node.metadata.graph.mediaType === "image")!.metadata!.graph,
    ).toMatchObject({ items: [{ displayName: "two.png" }, { displayName: "one.png" }] });
    await page.getByRole("button", { name: "移除 @图片2" }).click();
    expect([...document.querySelectorAll("p")].some((item) => item.textContent === "@图片2")).toBe(false);

    await expect.element(page.getByRole("button", { name: "图片生成" })).not.toBeDisabled();
    await page.getByRole("button", { name: "图片生成" }).click();
    await page.getByRole("button", { name: "视频生成" }).click();
    const modelNodes = useCanvasStore
        .getState()
        .openProject(projectId)!
        .nodes.filter((node) => node.metadata?.graph?.role === "model");
    expect(modelNodes.map((node) => node.metadata?.graph?.role === "model" && node.metadata.graph.modelId)).toEqual(["seedream-fixture", "seedance-fixture"]);
    const imageNode = document.querySelector(`[data-node-id="${modelNodes[0].id}"]`)!;
    const videoNode = document.querySelector(`[data-node-id="${modelNodes[1].id}"]`)!;
    (imageNode.querySelector('input[aria-label="count"]') as HTMLInputElement).value = "2";
    imageNode.querySelector('input[aria-label="count"]')!.dispatchEvent(new Event("change", { bubbles: true }));
    (videoNode.querySelector('input[aria-label="duration"]') as HTMLInputElement).value = "8";
    videoNode.querySelector('input[aria-label="duration"]')!.dispatchEvent(new Event("change", { bubbles: true }));

    const promptNode = useCanvasStore
        .getState()
        .openProject(projectId)!
        .nodes.find((node) => node.metadata?.graph?.role === "prompt")!;
    const imageCollection = useCanvasStore
        .getState()
        .openProject(projectId)!
        .nodes.find((node) => node.metadata?.graph?.role === "media-collection" && node.metadata.graph.mediaType === "image")!;
    const videoCollection = useCanvasStore
        .getState()
        .openProject(projectId)!
        .nodes.find((node) => node.metadata?.graph?.role === "media-collection" && node.metadata.graph.mediaType === "video")!;
    {
        const current = useCanvasStore.getState().openProject(projectId)!;
        useCanvasStore.getState().updateProject(projectId, {
            nodes: current.nodes.map((node) => {
                if (node.id === promptNode.id) return { ...node, position: { x: 0, y: 0 } };
                if (node.id === imageCollection.id) return { ...node, position: { x: 320, y: 0 } };
                if (node.id === modelNodes[0].id) return { ...node, position: { x: 680, y: 0 } };
                if (node.id === videoCollection.id) return { ...node, position: { x: 0, y: 330 } };
                if (node.id === modelNodes[1].id) return { ...node, position: { x: 680, y: 330 } };
                return node;
            }),
        });
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    }
    await page.getByTestId(`draggable-node-${promptNode.id}`).getByRole("button", { name: `${promptNode.title}：提示词输出端口` }).click();
    await page.getByRole("button", { name: `${modelNodes[0].title}：提示词输入端口` }).click();
    await page.getByRole("button", { name: `${imageCollection.title}：媒体输出端口` }).click();
    await page.getByRole("button", { name: `${modelNodes[0].title}：参考图片输入端口` }).click();
    await page.getByRole("button", { name: `${videoCollection.title}：媒体输出端口` }).click();
    await page.getByRole("button", { name: `${modelNodes[0].title}：参考图片输入端口` }).click();
    await expect.element(page.getByTestId("connection-status")).toHaveTextContent("端口类型不兼容");
    expect(useCanvasStore.getState().openProject(projectId)!.connections).toHaveLength(2);

    const connection = document.querySelector<SVGPathElement>('[data-connection-active="true"]')!;
    connection.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 500, clientY: 220 }));
    await expect.element(page.getByRole("menu", { name: "连接操作" })).toBeVisible();
    (document.querySelector('[role="menu"][aria-label="连接操作"] [role="menuitem"]') as HTMLButtonElement).click();
    expect(useCanvasStore.getState().openProject(projectId)!.connections).toHaveLength(1);

    const audioNode = useCanvasStore
        .getState()
        .openProject(projectId)!
        .nodes.find((node) => node.metadata?.graph?.role === "media-collection" && node.metadata.graph.mediaType === "audio")!;
    const audioElement = document.querySelector(`[data-node-id="${audioNode.id}"]`)!;
    audioElement.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 520, clientY: 500 }));
    await expect.element(page.getByRole("menu", { name: "节点操作" })).toBeVisible();
    (document.querySelector('[role="menu"][aria-label="节点操作"] [role="menuitem"]:last-child') as HTMLButtonElement).click();
    expect(
        useCanvasStore
            .getState()
            .openProject(projectId)!
            .nodes.some((node) => node.id === audioNode.id),
    ).toBe(false);

    {
        const current = useCanvasStore.getState().openProject(projectId)!;
        const withResult = appendJobResults(
            current.nodes,
            current.connections,
            { id: "fixture-result-job", operation: "image.generate", status: "succeeded", results: [{ url: offlineResultUrl, asset_id: "job-result.fixture-result-job.0", media_type: "image" }] },
            modelNodes[0],
        );
        useCanvasStore.getState().updateProject(projectId, {
            nodes: withResult.nodes.map((node) => (node.metadata?.sourceJobId === "fixture-result-job" ? { ...node, position: { x: 320, y: 330 } } : node)),
            connections: withResult.connections,
        });
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    }
    const resultNode = useCanvasStore
        .getState()
        .openProject(projectId)!
        .nodes.find((node) => node.metadata?.sourceJobId === "fixture-result-job")!;
    const resultGraph = resultNode.metadata!.graph!;
    if (resultGraph.role !== "result") throw new Error("fixture result node must expose a result graph contract");
    const modelResultPort = modelNodes[0].metadata!.graph!.outputPortId;
    const resultInputPort = resultGraph.inputPortId;
    expect(
        useCanvasStore
            .getState()
            .openProject(projectId)!
            .connections.some((edge) => edge.fromNodeId === modelNodes[0].id
                && edge.fromPortId === modelResultPort
                && edge.toNodeId === resultNode.id
                && edge.toPortId === resultInputPort),
    ).toBe(true);
    await expect.element(page.getByRole("button", { name: `连接：${modelNodes[0].title} 结果(result) 到 ${resultNode.title} 结果(result)` })).toBeVisible();
    await expect.element(page.getByRole("img", { name: "生成结果" })).toBeVisible();
    const preview = await page.getByRole("img", { name: "生成结果" }).element() as HTMLImageElement;
    const download = page.getByRole("link", { name: "下载" });
    await expect.element(download).toHaveAttribute("href", offlineResultUrl);
    const downloadResponse = await fetch((await download.element() as HTMLAnchorElement).getAttribute("href")!);
    expect(downloadResponse.status).toBe(200);
    expect(downloadResponse.headers.get("content-type")).toBe("image/png");
    const previewUrl = URL.createObjectURL(await downloadResponse.blob());
    preview.src = previewUrl;
    await expect.poll(() => preview.naturalWidth).toBeGreaterThan(0);
    URL.revokeObjectURL(previewUrl);
    await page.getByRole("button", { name: `${resultNode.title}：媒体输出端口` }).click();
    await page.getByRole("button", { name: `${modelNodes[1].title}：参考图片输入端口` }).click();
    expect(
        useCanvasStore
            .getState()
            .openProject(projectId)!
            .connections.some((edge) => edge.fromNodeId === resultNode.id && edge.toNodeId === modelNodes[1].id),
    ).toBe(true);

    const serialized = JSON.parse(JSON.stringify(useCanvasStore.getState().openProject(projectId)!));
    useCanvasStore.getState().replaceProjects([serialized]);
    expect(useCanvasStore.getState().openProject(projectId)).toMatchObject({ nodes: expect.any(Array), connections: expect.any(Array) });
    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(window.innerWidth);
});
