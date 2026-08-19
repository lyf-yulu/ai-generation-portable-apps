import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import CanvasProjectPage from "@/pages/canvas/project";
import { useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { clearStorageScope, setStorageScope } from "@/storage/scope";


afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); clearStorageScope(); useCanvasStore.setState({ projects: [], projectSyncMetadata: {}, syncNotice: null, hydrated: true, projectsLoaded: false }); });

function LocationProbe() {
    return <output data-testid="location">{useLocation().pathname}</output>;
}

function renderProject(id: string) {
    return render(<MemoryRouter initialEntries={[`/canvas/${id}`]}><Routes>
        <Route path="/canvas" element={<><div>project library</div><LocationProbe /></>} />
        <Route path="/canvas/:id" element={<><CanvasProjectPage /><LocationProbe /></>} />
    </Routes></MemoryRouter>);
}

it("waits for the server project list before redirecting a missing project", async () => {
    useCanvasStore.setState({ projects: [], projectsLoaded: false });

    renderProject("server-project");

    expect(screen.getByTestId("location")).toHaveTextContent("/canvas/server-project");
    useCanvasStore.getState().setProjectsLoaded(true);
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/canvas"));
});

it("assembles the released image and video generation studio around the infinite canvas", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const projectId = useCanvasStore.getState().createProject("黑绿工作室");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ models: [{ model_id: "demo-image-v1", service_id: "demo-image", display_name: "本地演示图片", operations: ["image.generate"], input_media: ["text"], input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }], parameter_mappings: { size: "size", ratio: "ratio" }, parameter_schema: { type: "object", properties: { size: { type: "string", default: "2K", title: "尺寸档位", "x-ark-size": { presets: ["1K", "1.5K", "2K"], min_pixels: 921600, max_pixels: 4624220, min_ratio: 0.0625, max_ratio: 16 } }, ratio: { type: "string", enum: ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9"], default: "1:1", title: "比例" } } } }] }), { headers: { "content-type": "application/json" } })));

    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);

    expect(screen.getByTestId("studio-palette")).toBeVisible();
    expect(screen.getByTestId("studio-canvas")).toBeVisible();
    expect(screen.getByTestId("studio-canvas")).toHaveClass("flex-1", "min-h-0");
    expect(screen.getByRole("link", { name: "返回项目列表" })).toHaveAttribute("href", "/canvas");
    expect(screen.queryByTestId("generation-inspector")).not.toBeInTheDocument();
    expect(screen.getByText("提示词节点")).toBeVisible();
    expect(screen.getByRole("button", { name: "图片生成" })).toBeVisible();
    expect(screen.getByRole("button", { name: "视频生成" })).toBeVisible();
    expect(screen.queryByText(/Dreamina|ComfyUI|Skill/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开人像资产库" })).toBeVisible();
    fireEvent.click(await screen.findByRole("button", { name: "图片生成" }));
    await waitFor(() => expect(screen.getByLabelText("模型")).toHaveValue("demo-image-v1"));
    expect(screen.getByRole("button", { name: "运行模型" })).toBeVisible();
});

it("uses the stored project viewport and exposes scale and reset controls", () => {
    const id = useCanvasStore.getState().createProject("Stored view");
    useCanvasStore.getState().updateProject(id, { viewport: { x: 120, y: -45, k: 1.75 } });

    renderProject(id);

    const resetControl = screen.getByRole("button", { name: "复位画布" });
    const scaleControl = screen.getByLabelText("画布缩放");

    expect(screen.getByTestId("canvas-world")).toHaveStyle({ transform: "translate(120px, -45px) scale(1.75)" });
    expect(scaleControl).toHaveValue("175");

    fireEvent.click(resetControl);

    expect(useCanvasStore.getState().openProject(id)?.viewport).toEqual({ x: 0, y: 0, k: 1 });
});

it("keeps the compact navigation-control class contract", () => {
    const id = useCanvasStore.getState().createProject("Narrow view");

    renderProject(id);

    const studioCanvas = screen.getByTestId("studio-canvas");
    const resetControl = screen.getByRole("button", { name: "复位画布" });
    const scaleControl = screen.getByLabelText("画布缩放");
    const navigationControls = resetControl.parentElement;
    expect(studioCanvas).toContainElement(navigationControls);
    expect(navigationControls).toHaveClass("left-4", "max-w-[calc(100%-2rem)]", "px-3", "py-2");
    expect(resetControl).toHaveClass("px-2", "py-1", "text-xs");
    expect(scaleControl).toHaveClass("w-20");
});

it("shows sync failure, conflict, and recovery notices without overlaying the canvas", () => {
    const id = useCanvasStore.getState().createProject("Notice lifecycle");
    const view = renderProject(id);

    act(() => useCanvasStore.getState().setSyncNotice("项目暂时无法同步，当前修改仍保留在本机。"));
    expect(screen.getByTestId("project-sync-notice")).toHaveTextContent("项目暂时无法同步");
    expect(screen.getByTestId("project-sync-notice")).not.toBe(screen.getByTestId("studio-canvas"));
    expect(screen.getByTestId("project-sync-notice").parentElement).toContainElement(screen.getByTestId("studio-canvas"));

    act(() => useCanvasStore.getState().setSyncNotice("检测到其他位置的更新，已保留一个冲突副本。"));
    expect(screen.getByTestId("project-sync-notice")).toHaveTextContent("冲突副本");

    act(() => useCanvasStore.getState().setSyncNotice("项目已恢复同步。"));
    expect(screen.getByTestId("project-sync-notice")).toHaveTextContent("项目已恢复同步");

    act(() => useCanvasStore.getState().setSyncNotice(null));
    expect(screen.queryByTestId("project-sync-notice")).not.toBeInTheDocument();
    view.unmount();
});
