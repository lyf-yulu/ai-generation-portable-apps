import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { DraggableCanvasNode } from "@/components/canvas/draggable-canvas-node";
import { MAX_NODE_SCALE, MIN_NODE_SCALE, NODE_SCALE_STEP } from "@/lib/canvas/node-scale";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    document.body.style.cursor = "";
});

function nodeAt(overrides: Partial<CanvasNodeData> = {}): CanvasNodeData {
    return { id: "node-a", type: CanvasNodeType.Text, title: "Node A", position: { x: 10, y: 20 }, width: 200, height: 100, ...overrides };
}

it("scales the node box and its content proportionally", () => {
    render(
        <DraggableCanvasNode node={nodeAt({ scale: 1.5 })} scale={1} onPositionChange={vi.fn()}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    const box = screen.getByTestId("draggable-node-node-a");
    expect(box.style.width).toBe("300px");
    expect(box.style.height).toBe("150px");
    const inner = screen.getByTestId("node-content-node-a");
    expect(inner.style.transform).toBe("scale(1.5)");
    expect(inner.style.transformOrigin).toBe("top left");
    expect(inner.style.width).toBe("200px");
    expect(inner.style.minHeight).toBe("100px");
});

it("reports the scaled measured size for connections and ports", () => {
    let resizeCallback!: ResizeObserverCallback;
    vi.stubGlobal(
        "ResizeObserver",
        class {
            constructor(callback: ResizeObserverCallback) { resizeCallback = callback; }
            observe = vi.fn();
            disconnect = vi.fn();
        },
    );
    const onMeasuredSize = vi.fn();
    render(
        <DraggableCanvasNode node={nodeAt({ scale: 1.5 })} scale={1} onPositionChange={vi.fn()} onMeasuredSize={onMeasuredSize}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    act(() => resizeCallback([{ contentRect: { width: 200, height: 100 } }] as ResizeObserverEntry[], {} as ResizeObserver));
    expect(onMeasuredSize).toHaveBeenCalledWith("node-a", { width: 300, height: 150 });
});

it("shows zoom controls on a selected node and reports scale changes", () => {
    const onScaleChange = vi.fn();
    render(
        <DraggableCanvasNode node={nodeAt({ scale: 1 })} scale={1} selected onPositionChange={vi.fn()} onScaleChange={onScaleChange}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    expect(screen.getByRole("button", { name: "重置节点缩放" })).toHaveTextContent("100%");
    fireEvent.click(screen.getByRole("button", { name: "放大节点" }));
    expect(onScaleChange).toHaveBeenLastCalledWith("node-a", 1 + NODE_SCALE_STEP);
    fireEvent.click(screen.getByRole("button", { name: "缩小节点" }));
    expect(onScaleChange).toHaveBeenLastCalledWith("node-a", 1 - NODE_SCALE_STEP);
    fireEvent.click(screen.getByRole("button", { name: "重置节点缩放" }));
    expect(onScaleChange).toHaveBeenLastCalledWith("node-a", 1);
});

it("keeps port overlays outside the scaled content wrapper so their stacking is not trapped", () => {
    render(
        <DraggableCanvasNode
            node={nodeAt({ scale: 2 })}
            scale={1}
            onPositionChange={vi.fn()}
            overlays={<button type="button" data-testid="port-overlay">port</button>}
        >
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    const box = screen.getByTestId("draggable-node-node-a");
    const content = screen.getByTestId("node-content-node-a");
    const overlay = screen.getByTestId("port-overlay");
    expect(box.contains(overlay)).toBe(true);
    expect(content.contains(overlay)).toBe(false);
});

it("hides zoom controls when the node is not selected", () => {
    render(
        <DraggableCanvasNode node={nodeAt({ scale: 1 })} scale={1} onPositionChange={vi.fn()} onScaleChange={vi.fn()}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    expect(screen.queryByRole("button", { name: "放大节点" })).toBeNull();
});

it("clamps node scale changes at the minimum and maximum", () => {
    const onScaleChange = vi.fn();
    const { rerender } = render(
        <DraggableCanvasNode node={nodeAt({ scale: MIN_NODE_SCALE })} scale={1} selected onPositionChange={vi.fn()} onScaleChange={onScaleChange}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    fireEvent.click(screen.getByRole("button", { name: "缩小节点" }));
    expect(onScaleChange).not.toHaveBeenCalled();
    rerender(
        <DraggableCanvasNode node={nodeAt({ scale: MAX_NODE_SCALE })} scale={1} selected onPositionChange={vi.fn()} onScaleChange={onScaleChange}>
            <span>inner content</span>
        </DraggableCanvasNode>,
    );
    fireEvent.click(screen.getByRole("button", { name: "放大节点" }));
    expect(onScaleChange).not.toHaveBeenCalled();
});
