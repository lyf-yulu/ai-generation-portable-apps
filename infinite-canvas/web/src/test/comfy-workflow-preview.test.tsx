import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { WorkflowPreview } from "@/components/comfy/workflow-preview";

afterEach(cleanup);

it("renders only generic node labels and never injects a widget value", () => {
    render(<WorkflowPreview preview={{ has_editor_layout: true, nodes: [{ id: "1", type: "LoadImage", title: "<img src=x>", position: [20, 40] }], edges: [] }} />);
    expect(screen.getByText("LoadImage")).toBeVisible();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.queryByText("<img src=x>")).not.toBeInTheDocument();
});

it("uses a semantic summary without editor layout and caps projected graph rendering", () => {
    const nodes = Array.from({ length: 501 }, (_, index) => ({ id: String(index), type: `Node ${index}`, title: null, position: null }));
    const edges = Array.from({ length: 2001 }, (_, index) => ({ source_id: String(index % 501), target_id: String((index + 1) % 501) }));
    render(<WorkflowPreview preview={{ has_editor_layout: false, nodes, edges }} />);
    expect(screen.getByRole("table", { name: "工作流节点摘要" })).toBeVisible();
    expect(screen.getByText("节点：501 · 连线：2001")).toBeVisible();
    expect(screen.getAllByRole("row")).toHaveLength(501);
});
