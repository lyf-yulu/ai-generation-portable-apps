import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import CanvasProjectPage from "@/pages/canvas/project";
import { useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { clearStorageScope, setStorageScope } from "@/storage/scope";

afterEach(() => { cleanup(); vi.restoreAllMocks(); clearStorageScope(); useCanvasStore.setState({ projects: [], projectSyncMetadata: {}, syncNotice: null, hydrated: true }); });

it("shows the regenerated result image after modifying the prompt, without remounting", async () => {
    await setStorageScope({ environment: "test", userId: "u-a" });
    const projectId = useCanvasStore.getState().createProject("Regen Canvas");
    vi.stubGlobal("fetch", vi.fn()
        .mockResolvedValueOnce(new Response(JSON.stringify({ models: [{ model_id: "image", service_id: "s", display_name: "Image", operations: ["image.generate"], input_media: ["text"], input_ports: [{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }], parameter_mappings: {}, parameter_schema: {} }] }), { headers: { "content-type": "application/json" } }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ id: "job-1", status: "queued" }), { status: 201, headers: { "content-type": "application/json" } }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ id: "job-1", status: "succeeded", result_url: "/api/v1/results/r-1" }), { headers: { "content-type": "application/json" } }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ id: "job-2", status: "queued" }), { status: 201, headers: { "content-type": "application/json" } }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ id: "job-2", status: "succeeded", result_url: "/api/v1/results/r-2" }), { headers: { "content-type": "application/json" } })));
    render(<MemoryRouter initialEntries={[`/canvas/${projectId}`]}><Routes><Route path="/canvas/:id" element={<CanvasProjectPage />} /></Routes></MemoryRouter>);

    fireEvent.click(screen.getByRole("button", { name: "提示词节点" }));
    const promptInput = screen.getByLabelText("提示词内容");
    fireEvent.change(promptInput, { target: { value: "a cat" } });
    fireEvent.click(await screen.findByRole("button", { name: "图片生成" }));
    act(() => {
        const project = useCanvasStore.getState().openProject(projectId)!;
        const prompt = project.nodes.find((node) => node.metadata?.graph?.role === "prompt")!;
        const model = project.nodes.find((node) => node.metadata?.graph?.role === "model")!;
        useCanvasStore.getState().updateProject(projectId, { connections: [{ id: "prompt-model", fromNodeId: prompt.id, fromPortId: "prompt", toNodeId: model.id, toPortId: "prompt" }] });
    });
    await waitFor(() => expect(screen.getByLabelText("模型")).toHaveValue("image"));
    fireEvent.click(screen.getByRole("button", { name: "运行模型" }));
    await waitFor(() => expect(useCanvasStore.getState().openProject(projectId)?.nodes.some((node) => node.metadata?.sourceJobId === "job-1")).toBe(true));
    expect(await screen.findByTestId("result-node-job-1")).toBeVisible();

    // Modify the prompt and regenerate on the same page (no remount).
    fireEvent.change(screen.getByLabelText("提示词内容"), { target: { value: "a dog" } });
    fireEvent.click(screen.getByRole("button", { name: "运行模型" }));

    // The new result node must appear in the UI without any navigation/remount.
    await waitFor(() => expect(useCanvasStore.getState().openProject(projectId)?.nodes.some((node) => node.metadata?.sourceJobId === "job-2")).toBe(true));
    expect(await screen.findByTestId("result-node-job-2")).toBeVisible();
    const result2 = useCanvasStore.getState().openProject(projectId)!.nodes.find((node) => node.metadata?.sourceJobId === "job-2")!;
    expect(result2.metadata?.content).toBe("/api/v1/results/r-2");
});
