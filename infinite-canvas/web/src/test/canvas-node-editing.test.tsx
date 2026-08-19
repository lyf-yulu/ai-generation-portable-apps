import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DraggableCanvasNode } from "@/components/canvas/draggable-canvas-node";
import { CanvasNodeContextMenu } from "@/components/canvas/canvas-context-menu";
import { PromptNodeCard } from "@/components/canvas/prompt-node-card";
import CanvasProjectPage from "@/pages/canvas/project";
import { useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { clearStorageScope, setScopedStoreFactoryForTest, setStorageScope } from "@/storage/scope";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from "@/types/canvas";

function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (reason?: unknown) => void;
    const promise = new Promise<T>((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
}

function fileWithRead(name: string, read: Promise<ArrayBuffer>) {
    const file = new File(["placeholder"], name, { type: "text/plain" });
    Object.defineProperty(file, "arrayBuffer", { configurable: true, value: () => read });
    return file;
}

function utf8(value: string) {
    return new TextEncoder().encode(value).buffer;
}

function node(id: string, x: number): CanvasNodeData {
    return {
        id,
        type: CanvasNodeType.Text,
        title: `Prompt ${id}`,
        position: { x, y: 80 },
        width: 280,
        height: 160,
        metadata: {
            content: id,
            status: "idle",
            graph: { schemaVersion: 1, role: "prompt", text: id, outputPortId: "prompt" },
        },
    };
}

function resultNode(id: string, x: number): CanvasNodeData {
    return {
        id,
        type: CanvasNodeType.Image,
        title: `Result ${id}`,
        position: { x, y: 120 },
        width: 320,
        height: 220,
        metadata: {
            status: "success",
            content: `/api/v1/results/${id}`,
            sourceJobId: `job-${id}`,
            graph: { schemaVersion: 1, role: "result", mediaType: "image", inputPortId: "result", outputPortId: "media", assetId: `asset-${id}`, jobId: `job-${id}` },
        },
    };
}

async function renderProject(nodes: CanvasNodeData[] = [], connections: CanvasConnection[] = [], models: unknown[] = []) {
    await setStorageScope({ environment: "test", userId: "editing-user" });
    const projectId = useCanvasStore.getState().createProject("Editing Canvas");
    useCanvasStore.getState().updateProject(projectId, { nodes, connections });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ models }), {
        headers: { "content-type": "application/json" },
    })));
    render(
        <MemoryRouter initialEntries={[`/canvas/${projectId}`]}>
            <Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes>
        </MemoryRouter>,
    );
    return projectId;
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

describe("project-scoped node selection and deletion", () => {
    it("lets content-sized nodes ignore their persisted minimum height", () => {
        render(
            <DraggableCanvasNode node={resultNode("adaptive", 80)} scale={1} contentSized onPositionChange={() => undefined}>
                <div style={{ height: 90 }}>adaptive</div>
            </DraggableCanvasNode>,
        );

        expect(screen.getByTestId("draggable-node-adaptive")).not.toHaveStyle({ minHeight: "220px" });
    });

    it("selects one node, adds another with a modifier, and clears selection on a background click", async () => {
        await renderProject([node("a", 80), node("b", 400)]);

        fireEvent.pointerDown(screen.getByTestId("draggable-node-a"), { button: 0, pointerId: 1 });
        fireEvent.pointerUp(window, { pointerId: 1 });
        expect(screen.getByTestId("draggable-node-a")).toHaveAttribute("aria-selected", "true");
        expect(screen.getByTestId("draggable-node-b")).toHaveAttribute("aria-selected", "false");

        fireEvent.pointerDown(screen.getByTestId("draggable-node-b"), { button: 0, pointerId: 2, metaKey: true });
        expect(screen.getByTestId("draggable-node-a")).toHaveAttribute("aria-selected", "true");
        expect(screen.getByTestId("draggable-node-b")).toHaveAttribute("aria-selected", "true");

        fireEvent.pointerDown(screen.getByTestId("infinite-canvas"), { button: 0, pointerId: 3 });
        fireEvent.pointerUp(window, { pointerId: 3 });
        expect(screen.getByTestId("draggable-node-a")).toHaveAttribute("aria-selected", "false");
        expect(screen.getByTestId("draggable-node-b")).toHaveAttribute("aria-selected", "false");
    });

    it("deletes all selected nodes and their incident connections with Delete", async () => {
        const projectId = await renderProject([node("a", 80), node("b", 400), node("c", 720)], [
            { id: "a-b", fromNodeId: "a", fromPortId: "prompt", toNodeId: "b", toPortId: "prompt" },
            { id: "c-b", fromNodeId: "c", fromPortId: "prompt", toNodeId: "b", toPortId: "prompt" },
        ]);
        fireEvent.pointerDown(screen.getByTestId("draggable-node-a"), { button: 0, pointerId: 1 });
        fireEvent.pointerDown(screen.getByTestId("draggable-node-b"), { button: 0, pointerId: 2, ctrlKey: true });

        fireEvent.keyDown(window, { key: "Delete" });

        expect(useCanvasStore.getState().openProject(projectId)?.nodes.map((item) => item.id)).toEqual(["c"]);
        expect(useCanvasStore.getState().openProject(projectId)?.connections).toEqual([]);
    });

    it("does not delete a selected node while any editable control owns the key event", async () => {
        const projectId = await renderProject([node("a", 80)]);
        fireEvent.pointerDown(screen.getByTestId("draggable-node-a"), { button: 0, pointerId: 1 });
        const contentEditor = document.createElement("div");
        contentEditor.setAttribute("contenteditable", "true");
        document.body.append(contentEditor);
        const controls = [
            screen.getByLabelText("提示词内容"),
            screen.getByLabelText("导入 TXT"),
            contentEditor,
        ];

        for (const control of controls) fireEvent.keyDown(control, { key: "Backspace" });

        expect(useCanvasStore.getState().openProject(projectId)?.nodes.map((item) => item.id)).toEqual(["a"]);
        contentEditor.remove();
    });

    it("selects an interactive control's node without dragging or deleting the prior selection", async () => {
        const projectId = await renderProject([node("a", 80), node("b", 400)]);
        const first = screen.getByTestId("draggable-node-a");
        const second = screen.getByTestId("draggable-node-b");
        fireEvent.pointerDown(first, { button: 0, pointerId: 1 });
        fireEvent.pointerUp(window, { pointerId: 1 });
        const editor = within(second).getByRole("textbox", { name: "提示词内容" });

        fireEvent.focus(editor);
        expect(first).toHaveAttribute("aria-selected", "false");
        expect(second).toHaveAttribute("aria-selected", "true");
        fireEvent.pointerDown(first, { button: 0, pointerId: 3 });
        fireEvent.pointerUp(window, { pointerId: 3 });

        fireEvent.pointerDown(editor, { button: 0, pointerId: 2, clientX: 410, clientY: 100 });
        fireEvent.focus(editor);
        fireEvent.pointerMove(window, { pointerId: 2, clientX: 510, clientY: 200 });
        fireEvent.pointerUp(window, { pointerId: 2 });

        expect(first).toHaveAttribute("aria-selected", "false");
        expect(second).toHaveAttribute("aria-selected", "true");
        expect(useCanvasStore.getState().openProject(projectId)?.nodes.find((item) => item.id === "b")?.position).toEqual({ x: 400, y: 80 });
        fireEvent.keyDown(editor, { key: "Delete" });
        expect(useCanvasStore.getState().openProject(projectId)?.nodes.map((item) => item.id)).toEqual(["a", "b"]);

        second.focus();
        fireEvent.keyDown(second, { key: "Delete" });
        expect(useCanvasStore.getState().openProject(projectId)?.nodes.map((item) => item.id)).toEqual(["a"]);
    });

    it("expires interactive focus suppression when an already-focused control's pointer gesture ends", async () => {
        const projectId = await renderProject([node("a", 80), node("b", 400)]);
        const first = screen.getByTestId("draggable-node-a");
        const second = screen.getByTestId("draggable-node-b");
        const editor = within(second).getByRole("textbox", { name: "提示词内容" });
        fireEvent.focus(editor);
        fireEvent.pointerDown(first, { button: 0, pointerId: 1 });
        fireEvent.pointerUp(window, { pointerId: 1 });
        expect(first).toHaveAttribute("aria-selected", "true");

        fireEvent.pointerDown(editor, { button: 0, pointerId: 2 });
        fireEvent.pointerUp(window, { pointerId: 2 });
        expect(second).toHaveAttribute("aria-selected", "true");
        fireEvent.pointerDown(first, { button: 0, pointerId: 3 });
        fireEvent.pointerUp(window, { pointerId: 3 });
        expect(first).toHaveAttribute("aria-selected", "true");

        fireEvent.focus(editor);
        expect(first).toHaveAttribute("aria-selected", "false");
        expect(second).toHaveAttribute("aria-selected", "true");
        second.focus();
        fireEvent.keyDown(second, { key: "Delete" });
        expect(useCanvasStore.getState().openProject(projectId)?.nodes.map((item) => item.id)).toEqual(["a"]);
    });

    it("selects prompt labels without dragging or blocking their native controls", async () => {
        const projectId = await renderProject([node("a", 80), node("b", 400)]);
        const first = screen.getByTestId("draggable-node-a");
        const second = screen.getByTestId("draggable-node-b");
        fireEvent.pointerDown(first, { button: 0, pointerId: 1 });
        fireEvent.pointerUp(window, { pointerId: 1 });
        const promptLabel = within(second).getByText("提示词内容", { selector: "label" });
        const fileLabel = within(second).getByText("导入 TXT", { selector: "label" });
        const promptEditor = within(second).getByRole("textbox", { name: "提示词内容" });
        const fileInput = within(second).getByLabelText("导入 TXT");

        const promptAllowed = fireEvent.pointerDown(promptLabel, { button: 0, pointerId: 2, clientX: 410, clientY: 100 });
        fireEvent.pointerMove(window, { pointerId: 2, clientX: 510, clientY: 200 });
        fireEvent.pointerUp(window, { pointerId: 2 });
        expect(promptAllowed).toBe(true);
        expect(first).toHaveAttribute("aria-selected", "false");
        expect(second).toHaveAttribute("aria-selected", "true");
        expect(useCanvasStore.getState().openProject(projectId)?.nodes.find((item) => item.id === "b")?.position).toEqual({ x: 400, y: 80 });
        expect((promptLabel as HTMLLabelElement).control).toBe(promptEditor);

        const fileActivated = vi.fn();
        fileInput.addEventListener("click", fileActivated);
        const fileAllowed = fireEvent.pointerDown(fileLabel, { button: 0, pointerId: 3 });
        fireEvent.pointerMove(window, { pointerId: 3, clientX: 600, clientY: 240 });
        fireEvent.pointerUp(window, { pointerId: 3 });
        fileLabel.click();
        expect(fileAllowed).toBe(true);
        expect((fileLabel as HTMLLabelElement).control).toBe(fileInput);
        expect(fileActivated).toHaveBeenCalledTimes(1);
        expect(useCanvasStore.getState().openProject(projectId)?.nodes.find((item) => item.id === "b")?.position).toEqual({ x: 400, y: 80 });

        fireEvent.pointerDown(first, { button: 0, pointerId: 4 });
        fireEvent.pointerUp(window, { pointerId: 4 });
        fireEvent.pointerDown(promptLabel, { button: 0, pointerId: 5, metaKey: true });
        fireEvent.pointerUp(window, { pointerId: 5 });
        fireEvent.focus(promptEditor);
        expect(first).toHaveAttribute("aria-selected", "true");
        expect(second).toHaveAttribute("aria-selected", "true");
    });

    it("clears interactive pointer suppression on pointer cancel and unmount", () => {
        const onSelect = vi.fn();
        const removeWindowListener = vi.spyOn(window, "removeEventListener");
        const view = render(
            <DraggableCanvasNode node={node("b", 400)} scale={1} onPositionChange={vi.fn()} onSelect={onSelect}>
                <textarea aria-label="gesture editor" />
            </DraggableCanvasNode>,
        );
        const editor = screen.getByRole("textbox", { name: "gesture editor" });
        fireEvent.pointerDown(editor, { button: 0, pointerId: 7 });
        expect(onSelect).toHaveBeenCalledTimes(1);
        fireEvent.pointerCancel(window, { pointerId: 7 });
        fireEvent.focus(editor);
        expect(onSelect).toHaveBeenCalledTimes(2);

        fireEvent.pointerDown(editor, { button: 0, pointerId: 8 });
        view.unmount();
        expect(removeWindowListener.mock.calls.map(([name]) => name)).toEqual(expect.arrayContaining(["pointerup", "pointercancel", "blur"]));
    });

    it("deletes a node and its incident connection from the node context menu", async () => {
        const projectId = await renderProject([node("a", 80), node("b", 400)], [
            { id: "a-b", fromNodeId: "a", fromPortId: "prompt", toNodeId: "b", toPortId: "prompt" },
        ]);

        fireEvent.contextMenu(screen.getByTestId("draggable-node-a"), { clientX: 42, clientY: 64 });
        fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));

        expect(useCanvasStore.getState().openProject(projectId)?.nodes.map((item) => item.id)).toEqual(["b"]);
        expect(useCanvasStore.getState().openProject(projectId)?.connections).toEqual([]);
    });

    it("keeps the node context menu inside a narrow viewport", async () => {
        await renderProject([node("a", 80)]);
        vi.stubGlobal("innerWidth", 360);
        vi.stubGlobal("innerHeight", 480);

        fireEvent.contextMenu(screen.getByTestId("draggable-node-a"), { clientX: 900, clientY: 900 });

        const menu = screen.getByRole("menuitem", { name: "删除" }).parentElement!;
        expect(Number.parseFloat(menu.style.left)).toBeLessThanOrEqual(176);
        expect(Number.parseFloat(menu.style.top)).toBeLessThanOrEqual(384);
    });

    it("remeasures menu height on resize instead of relying on a fixed item count", async () => {
        await renderProject([node("a", 80)]);
        vi.stubGlobal("innerWidth", 240);
        vi.stubGlobal("innerHeight", 240);
        let menuHeight = 80;
        const nativeRect = HTMLElement.prototype.getBoundingClientRect;
        vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
            if (this.getAttribute("role") === "menu") {
                return { x: 0, y: 0, left: 0, top: 0, right: 100, bottom: menuHeight, width: 100, height: menuHeight, toJSON: () => ({}) } as DOMRect;
            }
            return nativeRect.call(this);
        });

        fireEvent.contextMenu(screen.getByTestId("draggable-node-a"), { clientX: 230, clientY: 230 });
        const menu = screen.getByRole("menu", { name: "节点操作" });
        await waitFor(() => expect(menu.style.top).toBe("152px"));

        menuHeight = 180;
        fireEvent(window, new Event("resize"));
        await waitFor(() => expect(menu.style.top).toBe("52px"));
    });

    it("tracks visual viewport scroll and observed menu height, then cleans up", async () => {
        class TestViewport extends EventTarget {
            width = 240;
            height = 240;
            offsetLeft = 0;
            offsetTop = 0;
        }
        const viewport = new TestViewport();
        const addViewportListener = vi.spyOn(viewport, "addEventListener");
        const removeViewportListener = vi.spyOn(viewport, "removeEventListener");
        vi.stubGlobal("visualViewport", viewport);
        let resizeCallback!: ResizeObserverCallback;
        const observe = vi.fn();
        const disconnect = vi.fn();
        vi.stubGlobal("ResizeObserver", class {
            constructor(callback: ResizeObserverCallback) { resizeCallback = callback; }
            observe = observe;
            disconnect = disconnect;
        });
        vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
            const height = this.getAttribute("role") === "menu" ? this.querySelectorAll("[role='menuitem']").length * 60 : 0;
            return { x: 0, y: 0, left: 0, top: 0, right: 100, bottom: height, width: 100, height, toJSON: () => ({}) } as DOMRect;
        });
        const menu = { type: "node" as const, nodeId: "a", x: 230, y: 230 };
        const view = render(<CanvasNodeContextMenu menu={menu} onClose={vi.fn()} onDelete={vi.fn()} />);
        const surface = screen.getByRole("menu", { name: "节点操作" });
        await waitFor(() => expect(surface.style.top).toBe("172px"));
        expect(observe).toHaveBeenCalledWith(surface);
        expect(addViewportListener).toHaveBeenCalledWith("scroll", expect.any(Function));

        view.rerender(<CanvasNodeContextMenu menu={menu} onClose={vi.fn()} onDelete={vi.fn()} onDuplicate={vi.fn()} />);
        act(() => resizeCallback([], {} as ResizeObserver));
        await waitFor(() => expect(surface.style.top).toBe("112px"));
        viewport.offsetTop = 20;
        act(() => viewport.dispatchEvent(new Event("scroll")));
        await waitFor(() => expect(surface.style.top).toBe("132px"));

        view.unmount();
        expect(removeViewportListener).toHaveBeenCalledWith("scroll", expect.any(Function));
        expect(disconnect).toHaveBeenCalledTimes(1);
    });

    it.each([
        ["ContextMenu", { key: "ContextMenu" }],
        ["Shift+F10", { key: "F10", shiftKey: true }],
    ])("opens an accessible menu with %s and restores node focus on Escape", async (_name, shortcut) => {
        await renderProject([node("a", 80)]);
        const trigger = screen.getByTestId("draggable-node-a");
        trigger.focus();

        fireEvent.keyDown(trigger, shortcut);

        expect(screen.getByRole("menu", { name: "节点操作" })).toBeVisible();
        const firstItem = screen.getByRole("menuitem", { name: "复制" });
        await waitFor(() => expect(firstItem).toHaveFocus());
        fireEvent.keyDown(firstItem, { key: "Escape" });
        expect(screen.queryByRole("menu")).not.toBeInTheDocument();
        expect(trigger).toHaveFocus();
    });

    it("closes the context menu on Tab and on an outside pointer without trapping focus", async () => {
        await renderProject([node("a", 80)]);
        const trigger = screen.getByTestId("draggable-node-a");
        trigger.focus();
        fireEvent.keyDown(trigger, { key: "ContextMenu" });
        const item = await screen.findByRole("menuitem", { name: "删除" });

        fireEvent.keyDown(item, { key: "Tab" });
        expect(screen.queryByRole("menu")).not.toBeInTheDocument();

        fireEvent.keyDown(trigger, { key: "ContextMenu" });
        expect(screen.getByRole("menu")).toBeInTheDocument();
        fireEvent.pointerDown(document.body);
        expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    });
});

describe("canvas editing shortcuts", () => {
    it("copies a multi-selection with its internal connection and pastes fresh offset nodes", async () => {
        const projectId = await renderProject([resultNode("a", 80), resultNode("b", 420), resultNode("outside", 760)], [
            { id: "inside", fromNodeId: "a", fromPortId: "media", toNodeId: "b", toPortId: "result" },
            { id: "outside", fromNodeId: "a", fromPortId: "media", toNodeId: "outside", toPortId: "result" },
        ]);
        fireEvent.pointerDown(screen.getByTestId("draggable-node-a"), { button: 0, pointerId: 1 });
        fireEvent.pointerUp(window, { pointerId: 1 });
        fireEvent.pointerDown(screen.getByTestId("draggable-node-b"), { button: 0, pointerId: 2, metaKey: true });

        fireEvent.keyDown(window, { key: "c", metaKey: true });
        fireEvent.keyDown(window, { key: "v", metaKey: true });

        const current = useCanvasStore.getState().openProject(projectId)!;
        expect(current.nodes).toHaveLength(5);
        const pasted = current.nodes.slice(-2);
        expect(pasted.map((item) => item.position)).toEqual([{ x: 112, y: 152 }, { x: 452, y: 152 }]);
        expect(new Set(pasted.map((item) => item.id)).size).toBe(2);
        expect(current.connections).toHaveLength(3);
        expect(current.connections.at(-1)).toMatchObject({ fromNodeId: pasted[0].id, toNodeId: pasted[1].id, fromPortId: "media", toPortId: "result" });
        expect(screen.getByTestId(`draggable-node-${pasted[0].id}`)).toHaveAttribute("aria-selected", "true");
        expect(screen.getByTestId("canvas-command-status")).toHaveTextContent("已粘贴 2 个节点");
    });

    it("cuts then restores a node and supports Ctrl+A followed by Delete", async () => {
        const projectId = await renderProject([resultNode("a", 80), resultNode("b", 420)]);
        fireEvent.pointerDown(screen.getByTestId("draggable-node-a"), { button: 0, pointerId: 1 });
        fireEvent.pointerUp(window, { pointerId: 1 });

        fireEvent.keyDown(window, { key: "x", ctrlKey: true });
        expect(useCanvasStore.getState().openProject(projectId)?.nodes.map((item) => item.id)).toEqual(["b"]);
        fireEvent.keyDown(window, { key: "v", ctrlKey: true });
        expect(useCanvasStore.getState().openProject(projectId)?.nodes).toHaveLength(2);

        fireEvent.keyDown(window, { key: "a", ctrlKey: true });
        expect(screen.getAllByTestId(/draggable-node-/).every((item) => item.getAttribute("aria-selected") === "true")).toBe(true);
        fireEvent.keyDown(window, { key: "Delete" });
        expect(useCanvasStore.getState().openProject(projectId)?.nodes).toEqual([]);
    });

    it("preserves native clipboard and select-all behavior in editable controls", async () => {
        const projectId = await renderProject([node("prompt", 80)]);
        const editor = screen.getByRole("textbox", { name: "提示词内容" });
        fireEvent.pointerDown(screen.getByTestId("draggable-node-prompt"), { button: 0, pointerId: 1 });
        fireEvent.pointerUp(window, { pointerId: 1 });

        for (const key of ["c", "x", "v", "a"]) {
            expect(fireEvent.keyDown(editor, { key, metaKey: true })).toBe(true);
        }

        expect(useCanvasStore.getState().openProject(projectId)?.nodes.map((item) => item.id)).toEqual(["prompt"]);
        expect(screen.getByTestId("draggable-node-prompt")).toHaveAttribute("aria-selected", "true");
    });

    it("renames the single selected node with F2 and rejects a blank title", async () => {
        const projectId = await renderProject([resultNode("a", 80)]);
        const trigger = screen.getByTestId("draggable-node-a");
        fireEvent.pointerDown(trigger, { button: 0, pointerId: 1 });
        fireEvent.pointerUp(window, { pointerId: 1 });

        fireEvent.keyDown(window, { key: "F2" });
        const title = screen.getByRole("textbox", { name: "节点名称" });
        expect(title).toHaveValue("Result a");
        fireEvent.change(title, { target: { value: "   " } });
        expect(screen.getByRole("button", { name: "保存名称" })).toBeDisabled();
        fireEvent.change(title, { target: { value: "  首帧结果  " } });
        fireEvent.click(screen.getByRole("button", { name: "保存名称" }));

        expect(useCanvasStore.getState().openProject(projectId)?.nodes[0].title).toBe("首帧结果");
        expect(screen.getByText("首帧结果")).toBeVisible();
        await waitFor(() => expect(trigger).toHaveFocus());
    });

    it("keeps a multi-selection when opening a selected node menu and exposes copy, cut and rename", async () => {
        await renderProject([resultNode("a", 80), resultNode("b", 420)]);
        fireEvent.pointerDown(screen.getByTestId("draggable-node-a"), { button: 0, pointerId: 1 });
        fireEvent.pointerUp(window, { pointerId: 1 });
        fireEvent.pointerDown(screen.getByTestId("draggable-node-b"), { button: 0, pointerId: 2, shiftKey: true });

        fireEvent.contextMenu(screen.getByTestId("draggable-node-b"), { clientX: 450, clientY: 140 });

        expect(screen.getByTestId("draggable-node-a")).toHaveAttribute("aria-selected", "true");
        expect(screen.getByTestId("draggable-node-b")).toHaveAttribute("aria-selected", "true");
        const menu = screen.getByRole("menu", { name: "节点操作" });
        const copy = within(menu).getByRole("menuitem", { name: "复制" });
        const cut = within(menu).getByRole("menuitem", { name: "剪切" });
        expect(copy).toBeVisible();
        expect(cut).toBeVisible();
        expect(within(menu).getByRole("menuitem", { name: "重命名" })).toBeVisible();
        await waitFor(() => expect(copy).toHaveFocus());
        fireEvent.keyDown(copy, { key: "ArrowDown" });
        expect(cut).toHaveFocus();
    });
});

describe("blank canvas creation menu", () => {
    it("creates only the generic ComfyUI workflow node from the actual canvas context menu", async () => {
        const projectId = await renderProject();
        fireEvent.contextMenu(screen.getByTestId("infinite-canvas"), { clientX: 320, clientY: 210 });
        const menu = screen.getByRole("menu", { name: "创建节点" });

        fireEvent.click(within(menu).getByRole("menuitem", { name: "ComfyUI 工作流" }));

        const created = useCanvasStore.getState().openProject(projectId)?.nodes.find((item) => item.type === "comfy.workflow");
        expect(created?.metadata?.graph).toEqual({
            schemaVersion: 1,
            role: "comfy-workflow",
            workflowId: "unassigned",
            workflowRevision: 1,
            inputPorts: [],
            outputPortId: "result",
            executionEnabled: false,
        });
        expect(created?.type).toBe("comfy.workflow");
        expect(created?.type).not.toBe("MiniMaxH3ImageToVideo");
    });

    it("creates an image edit model node from the canvas menu when it is the assigned image capability", async () => {
        const projectId = await renderProject([], [], [{
            model_id: "edit-only",
            service_id: "image-service",
            display_name: "Edit only",
            operations: ["image.edit"],
            input_media: ["text", "image"],
            parameter_schema: {},
        }]);
        await waitFor(() => expect(screen.getByRole("button", { name: "图片生成" })).toBeEnabled());

        const canvas = screen.getByTestId("infinite-canvas");
        fireEvent.contextMenu(canvas, { clientX: 320, clientY: 210 });
        const menu = screen.getByRole("menu", { name: "创建节点" });
        const imageGeneration = within(menu).getByRole("menuitem", { name: "图片生成" });
        expect(imageGeneration).toBeEnabled();
        fireEvent.click(imageGeneration);

        const created = useCanvasStore.getState().openProject(projectId)?.nodes.find((item) => item.metadata?.graph?.role === "model");
        expect(created?.metadata?.graph).toMatchObject({ role: "model", operation: "image.edit" });
        expect(screen.getByTestId(`draggable-node-${created?.id}`)).toBeVisible();
    });

    it("creates the chosen node at the exact world position after pan and zoom", async () => {
        const projectId = await renderProject();
        useCanvasStore.getState().updateProject(projectId, { viewport: { x: 100, y: 50, k: 2 } });
        await waitFor(() => expect(screen.getByTestId("canvas-world")).toHaveStyle({ transform: "translate(100px, 50px) scale(2)" }));
        const canvas = screen.getByTestId("infinite-canvas");
        Object.defineProperty(canvas, "getBoundingClientRect", { configurable: true, value: () => ({ left: 20, top: 10, right: 1020, bottom: 710, width: 1000, height: 700, x: 20, y: 10, toJSON: () => ({}) }) });

        expect(fireEvent.contextMenu(canvas, { clientX: 320, clientY: 210 })).toBe(false);
        const menu = screen.getByRole("menu", { name: "创建节点" });
        expect(within(menu).getByRole("menuitem", { name: "图片生成" })).toBeDisabled();
        fireEvent.click(within(menu).getByRole("menuitem", { name: "参考图片" }));

        const created = useCanvasStore.getState().openProject(projectId)?.nodes[0];
        expect(created?.metadata?.graph).toMatchObject({ role: "media-collection", mediaType: "image" });
        expect(created?.position).toEqual({ x: 100, y: 75 });
        expect(screen.getByTestId(`draggable-node-${created?.id}`)).toHaveAttribute("aria-selected", "true");
    });

    it("offers all built-in node choices, allows another prompt and supports keyboard creation", async () => {
        const projectId = await renderProject([node("prompt", 80)]);
        const canvas = screen.getByTestId("infinite-canvas");
        fireEvent.contextMenu(canvas, { clientX: 240, clientY: 180 });
        const menu = screen.getByRole("menu", { name: "创建节点" });

        const prompt = within(menu).getByRole("menuitem", { name: "提示词" });
        expect(prompt).toBeEnabled();
        expect(within(menu).getByRole("menuitem", { name: "参考图片" })).toBeVisible();
        expect(within(menu).getByRole("menuitem", { name: "参考视频" })).toBeVisible();
        expect(within(menu).getByRole("menuitem", { name: "参考音频" })).toBeVisible();
        expect(within(menu).getByRole("menuitem", { name: "图片生成" })).toBeDisabled();
        expect(within(menu).getByRole("menuitem", { name: "视频生成" })).toBeDisabled();

        fireEvent.click(prompt);
        expect(useCanvasStore.getState().openProject(projectId)?.nodes.filter((item) => item.metadata?.graph?.role === "prompt")).toHaveLength(2);

        fireEvent.contextMenu(canvas, { clientX: 260, clientY: 200 });
        const reopened = screen.getByRole("menu", { name: "创建节点" });
        const firstEnabled = within(reopened).getByRole("menuitem", { name: "提示词" });
        await waitFor(() => expect(firstEnabled).toHaveFocus());
        fireEvent.keyDown(firstEnabled, { key: "ArrowDown" });
        fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
        const video = within(reopened).getByRole("menuitem", { name: "参考视频" });
        expect(video).toHaveFocus();
        fireEvent.keyDown(video, { key: "Enter" });
        expect(useCanvasStore.getState().openProject(projectId)?.nodes.at(-1)?.metadata?.graph).toMatchObject({ role: "media-collection", mediaType: "video" });
    });

    it("preserves the native blank-canvas context menu in read-only mode", async () => {
        await renderProject([node("protected", 80)]);
        useCanvasStore.setState({ loadError: { code: "UNSUPPORTED_GRAPH_SCHEMA", message: "需要升级应用", readOnly: true } });
        await waitFor(() => expect(screen.getByRole("textbox", { name: "提示词内容" })).toBeDisabled());
        const canvas = screen.getByTestId("infinite-canvas");

        expect(fireEvent.contextMenu(canvas, { clientX: 200, clientY: 160 })).toBe(true);
        expect(screen.queryByRole("menu", { name: "创建节点" })).not.toBeInTheDocument();
    });

    it("closes the creation menu with Escape and restores focus to the canvas", async () => {
        await renderProject();
        const canvas = screen.getByTestId("infinite-canvas");
        fireEvent.contextMenu(canvas, { clientX: 200, clientY: 160 });
        const item = screen.getByRole("menuitem", { name: "提示词" });
        await waitFor(() => expect(item).toHaveFocus());

        fireEvent.keyDown(item, { key: "Escape" });

        expect(screen.queryByRole("menu", { name: "创建节点" })).not.toBeInTheDocument();
        await waitFor(() => expect(canvas).toHaveFocus());
    });
});

describe("prompt node editing", () => {
    it("creates multiple independent prompt nodes from the palette", async () => {
        const projectId = await renderProject();
        const create = screen.getByRole("button", { name: "提示词节点" });
        fireEvent.click(create);
        expect(create).toBeEnabled();
        fireEvent.click(create);

        expect(useCanvasStore.getState().openProject(projectId)?.nodes.filter((item) => item.metadata?.graph?.role === "prompt")).toHaveLength(2);
        expect(screen.getAllByRole("textbox", { name: "提示词内容" })).toHaveLength(2);
    });

    it("creates one blank editable prompt without starting a job or showing a spinner", async () => {
        const projectId = await renderProject();
        fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));

        const editor = screen.getByRole("textbox", { name: "提示词内容" });
        expect(editor).toHaveValue("");
        expect(screen.queryByTestId(/generation-node-/)).not.toBeInTheDocument();
        expect(document.querySelector(".animate-spin")).not.toBeInTheDocument();
        const prompt = useCanvasStore.getState().openProject(projectId)?.nodes[0];
        expect(prompt?.metadata?.graph).toMatchObject({ role: "prompt", text: "" });
        expect((fetch as ReturnType<typeof vi.fn>).mock.calls.filter(([url]) => String(url) === "/api/v1/jobs")).toHaveLength(0);

        fireEvent.change(editor, { target: { value: "雾中的未来城市" } });
        expect(useCanvasStore.getState().openProject(projectId)?.nodes[0].metadata?.graph).toMatchObject({ role: "prompt", text: "雾中的未来城市" });
    });

    it("imports a local UTF-8 txt file into the same persisted prompt field", async () => {
        const projectId = await renderProject();
        fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));
        const file = new File(["第一幕：绿色雨夜"], "prompt.txt", { type: "text/plain" });

        fireEvent.change(screen.getByLabelText("导入 TXT"), { target: { files: [file] } });

        await waitFor(() => expect(screen.getByRole("textbox", { name: "提示词内容" })).toHaveValue("第一幕：绿色雨夜"));
        expect(useCanvasStore.getState().openProject(projectId)?.nodes[0].metadata?.graph).toMatchObject({ role: "prompt", text: "第一幕：绿色雨夜" });
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it.each([
        ["wrong type", new File(["hello"], "prompt.md", { type: "text/markdown" }), /TXT/],
        ["oversized", new File([new Uint8Array(1_048_577)], "large.txt", { type: "text/plain" }), /1 MB/],
        ["invalid UTF-8", new File([new Uint8Array([0xc3, 0x28])], "broken.txt", { type: "text/plain" }), /UTF-8/],
    ])("shows a visible error for %s imports without replacing text", async (_name, file, message) => {
        await renderProject();
        fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));
        const editor = screen.getByRole("textbox", { name: "提示词内容" });
        fireEvent.change(editor, { target: { value: "keep me" } });

        fireEvent.change(screen.getByLabelText("导入 TXT"), { target: { files: [file] } });

        expect(await screen.findByRole("alert")).toHaveTextContent(message);
        expect(editor).toHaveValue("keep me");
    });

    it("keeps the newest successful import when an older import later fails", async () => {
        const first = deferred<ArrayBuffer>();
        const second = deferred<ArrayBuffer>();
        const onTextChange = vi.fn();
        render(<PromptNodeCard node={node("a", 80)} onTextChange={onTextChange} />);

        fireEvent.change(screen.getByLabelText("导入 TXT"), { target: { files: [fileWithRead("a.txt", first.promise)] } });
        fireEvent.change(screen.getByLabelText("导入 TXT"), { target: { files: [fileWithRead("b.txt", second.promise)] } });
        await act(async () => second.resolve(utf8("newest")));
        expect(onTextChange).toHaveBeenCalledTimes(1);
        expect(onTextChange).toHaveBeenLastCalledWith("newest");

        await act(async () => first.reject(new Error("old read failed")));
        expect(onTextChange).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("keeps the newest import error when an older successful read finishes later", async () => {
        const first = deferred<ArrayBuffer>();
        const second = deferred<ArrayBuffer>();
        const onTextChange = vi.fn();
        render(<PromptNodeCard node={node("a", 80)} onTextChange={onTextChange} />);

        fireEvent.change(screen.getByLabelText("导入 TXT"), { target: { files: [fileWithRead("a.txt", first.promise)] } });
        fireEvent.change(screen.getByLabelText("导入 TXT"), { target: { files: [fileWithRead("b.txt", second.promise)] } });
        await act(async () => second.resolve(new Uint8Array([0xc3, 0x28]).buffer));
        expect(screen.getByRole("alert")).toHaveTextContent("UTF-8");

        await act(async () => first.resolve(utf8("stale success")));
        expect(onTextChange).not.toHaveBeenCalled();
        expect(screen.getByRole("alert")).toHaveTextContent("UTF-8");
    });

    it("does not publish a pending import after the prompt node identity changes", async () => {
        const pending = deferred<ArrayBuffer>();
        const onTextChange = vi.fn();
        const view = render(<PromptNodeCard node={node("a", 80)} onTextChange={onTextChange} />);
        fireEvent.change(screen.getByLabelText("导入 TXT"), { target: { files: [fileWithRead("a.txt", pending.promise)] } });

        view.rerender(<PromptNodeCard node={node("b", 80)} onTextChange={onTextChange} />);
        await act(async () => pending.resolve(utf8("wrong node")));

        expect(onTextChange).not.toHaveBeenCalled();
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("does not publish or set an error after unmounting with a read in flight", async () => {
        const pending = deferred<ArrayBuffer>();
        const onTextChange = vi.fn();
        const view = render(<PromptNodeCard node={node("a", 80)} onTextChange={onTextChange} />);
        fireEvent.change(screen.getByLabelText("导入 TXT"), { target: { files: [fileWithRead("a.txt", pending.promise)] } });
        view.unmount();

        await act(async () => pending.reject(new Error("after unmount")));

        expect(onTextChange).not.toHaveBeenCalled();
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("invalidates a successful read when disabled and never revives it after re-enable", async () => {
        const pending = deferred<ArrayBuffer>();
        const onTextChange = vi.fn();
        const view = render(<PromptNodeCard node={node("a", 80)} onTextChange={onTextChange} />);
        fireEvent.change(screen.getByLabelText("导入 TXT"), { target: { files: [fileWithRead("a.txt", pending.promise)] } });

        view.rerender(<PromptNodeCard disabled node={node("a", 80)} onTextChange={onTextChange} />);
        view.rerender(<PromptNodeCard node={node("a", 80)} onTextChange={onTextChange} />);
        await act(async () => pending.resolve(utf8("must stay stale")));

        expect(onTextChange).not.toHaveBeenCalled();
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("does not publish a read error after the prompt becomes disabled", async () => {
        const pending = deferred<ArrayBuffer>();
        const onTextChange = vi.fn();
        const view = render(<PromptNodeCard node={node("a", 80)} onTextChange={onTextChange} />);
        fireEvent.change(screen.getByLabelText("导入 TXT"), { target: { files: [fileWithRead("a.txt", pending.promise)] } });

        view.rerender(<PromptNodeCard disabled node={node("a", 80)} onTextChange={onTextChange} />);
        await act(async () => pending.reject(new Error("disabled read")));

        expect(onTextChange).not.toHaveBeenCalled();
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
});

it("keeps prompt editing and deletion disabled when graph loading entered read-only protection", async () => {
    const projectId = await renderProject([node("protected", 80)]);
    useCanvasStore.setState({
        loadError: { code: "UNSUPPORTED_GRAPH_SCHEMA", message: "需要升级应用", readOnly: true },
    });
    fireEvent.pointerDown(screen.getByTestId("draggable-node-protected"), { button: 0, pointerId: 1 });
    fireEvent.keyDown(screen.getByTestId("draggable-node-protected"), { key: "Enter" });
    const editor = screen.getByRole("textbox", { name: "提示词内容" });
    const promptLabel = within(screen.getByTestId("draggable-node-protected")).getByText("提示词内容", { selector: "label" });
    expect(fireEvent.pointerDown(promptLabel, { button: 0, pointerId: 2 })).toBe(true);
    fireEvent.pointerMove(window, { pointerId: 2, clientX: 100, clientY: 100 });
    fireEvent.pointerUp(window, { pointerId: 2 });

    expect(editor).toBeDisabled();
    fireEvent.change(editor, { target: { value: "must not persist" } });
    fireEvent.keyDown(window, { key: "Delete" });
    fireEvent.contextMenu(screen.getByTestId("draggable-node-protected"));

    expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByTestId("draggable-node-protected")).toHaveAttribute("aria-selected", "false");
    expect(useCanvasStore.getState().openProject(projectId)?.nodes[0].metadata?.graph).toMatchObject({ role: "prompt", text: "protected" });
});

it("preserves native context menus for editable descendants and read-only nodes", () => {
    const onContextMenu = vi.fn();
    const onSelect = vi.fn();
    const view = render(
        <DraggableCanvasNode node={node("editable", 80)} scale={1} onPositionChange={vi.fn()} onContextMenu={onContextMenu} onSelect={onSelect}>
            <textarea aria-label="node editor" />
        </DraggableCanvasNode>,
    );

    expect(fireEvent.contextMenu(screen.getByRole("textbox", { name: "node editor" }))).toBe(true);
    expect(onContextMenu).not.toHaveBeenCalled();

    view.rerender(
        <DraggableCanvasNode disabled node={node("editable", 80)} scale={1} onPositionChange={vi.fn()} onContextMenu={onContextMenu} onSelect={onSelect}>
            <span>read only surface</span>
        </DraggableCanvasNode>,
    );
    expect(fireEvent.contextMenu(screen.getByText("read only surface"))).toBe(true);
    fireEvent.pointerDown(screen.getByTestId("draggable-node-editable"), { button: 0, pointerId: 2 });
    fireEvent.keyDown(screen.getByTestId("draggable-node-editable"), { key: "Enter" });
    expect(onContextMenu).not.toHaveBeenCalled();
    expect(onSelect).not.toHaveBeenCalled();
});

it("does not drag from contenteditable descendants", () => {
    const onPositionChange = vi.fn();
    render(
        <DraggableCanvasNode node={node("editable", 80)} scale={1} onPositionChange={onPositionChange}>
            <div contentEditable suppressContentEditableWarning>editable text</div>
        </DraggableCanvasNode>,
    );
    const editor = screen.getByText("editable text");

    fireEvent.pointerDown(editor, { button: 0, pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 50, clientY: 50 });
    fireEvent.pointerUp(window, { pointerId: 1 });

    expect(onPositionChange).not.toHaveBeenCalled();
});
