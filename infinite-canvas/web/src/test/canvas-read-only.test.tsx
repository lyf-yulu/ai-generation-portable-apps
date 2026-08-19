import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "antd";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import CanvasPage from "@/pages/canvas";
import CanvasProjectPage from "@/pages/canvas/project";
import { clearCanvasInMemory, useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { useCanvasUiStore } from "@/stores/canvas/use-canvas-ui-store";
import { CanvasNodeType } from "@/types/canvas";

function LocationProbe() {
    const location = useLocation();
    return <output data-testid="readonly-location">{location.pathname}{location.search}</output>;
}

function protectCanvasStore() {
    useCanvasStore.setState({
        hydrated: true,
        projectsLoaded: false,
        loadError: { code: "UNSUPPORTED_GRAPH_SCHEMA", message: "此画布由更新版本创建，请升级应用后再编辑。", readOnly: true },
    });
}

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearCanvasInMemory();
    useCanvasUiStore.setState({ selectedProjectIds: [], deleteProjectIds: [], editingProjectId: null, editingProjectTitle: "" });
});

it.each(["new", "recent"])("shows the upgrade error and does not auto-create or loop for mode=%s", async (mode) => {
    const id = useCanvasStore.getState().createProject("Existing");
    protectCanvasStore();
    const before = useCanvasStore.getState().projects;

    render(<App><MemoryRouter initialEntries={[`/canvas?mode=${mode}`]}><Routes>
        <Route path="/canvas" element={<><CanvasPage /><LocationProbe /></>} />
        <Route path="/canvas/:id" element={<><div>opened project</div><LocationProbe /></>} />
    </Routes></MemoryRouter></App>);

    expect(screen.getByRole("alert")).toHaveTextContent("升级应用");
    expect(screen.getByRole("button", { name: "新建画布" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "导入画布" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "删除全部" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重命名" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "删除" })).toBeDisabled();
    expect(screen.getByTestId("readonly-location")).toHaveTextContent(`/canvas?mode=${mode}`);
    await waitFor(() => expect(useCanvasStore.getState().projects).toBe(before));
    expect(useCanvasStore.getState().openProject(id)).not.toBeNull();
});

it("shows a read-only banner and blocks project-page generation and node edits", async () => {
    const id = useCanvasStore.getState().createProject("Protected project");
    protectCanvasStore();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ models: [] }), { headers: { "content-type": "application/json" } })));

    render(<MemoryRouter initialEntries={[`/canvas/${id}`]}><Routes>
        <Route path="/canvas/:id" element={<CanvasProjectPage />} />
    </Routes></MemoryRouter>);

    expect(screen.getByRole("alert")).toHaveTextContent("升级应用");
    expect(screen.getByRole("button", { name: "提示词节点" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "图片生成" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "视频生成" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "运行模型" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));
    expect(useCanvasStore.getState().openProject(id)?.nodes).toEqual([]);
});

it("does not expose or invoke the paid retry path for failed nodes in read-only mode", async () => {
    const id = useCanvasStore.getState().createProject("Protected retry");
    useCanvasStore.getState().updateProject(id, {
        nodes: [{
            id: "failed-result",
            type: CanvasNodeType.Image,
            title: "生成失败",
            position: { x: 80, y: 80 },
            width: 320,
            height: 180,
            metadata: { status: "error", errorDetails: "safe failure", idempotencyKey: "retry-paid-request" },
        }],
    });
    protectCanvasStore();
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/v1/models")) return new Response(JSON.stringify({ models: [] }), { headers: { "content-type": "application/json" } });
        return new Response(JSON.stringify({ id: "must-not-create" }), { headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MemoryRouter initialEntries={[`/canvas/${id}`]}><Routes>
        <Route path="/canvas/:id" element={<CanvasProjectPage />} />
    </Routes></MemoryRouter>);

    const retry = screen.queryByRole("button", { name: "重试" });
    if (retry) fireEvent.click(retry);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input, init]) => String(input).includes("/api/v1/jobs") && init?.method === "POST")).toHaveLength(0);
});

it("shows the load error instead of an endless loader when the protected project cannot be materialized", () => {
    protectCanvasStore();
    render(<MemoryRouter initialEntries={["/canvas/future-project"]}><Routes>
        <Route path="/canvas/:id" element={<CanvasProjectPage />} />
    </Routes></MemoryRouter>);

    expect(screen.getByRole("alert")).toHaveTextContent("升级应用");
    expect(screen.queryByText("正在加载画布…")).not.toBeInTheDocument();
});
