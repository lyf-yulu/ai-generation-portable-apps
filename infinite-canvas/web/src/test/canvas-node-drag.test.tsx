import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { DraggableCanvasNode } from "@/components/canvas/draggable-canvas-node";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    document.body.style.cursor = "";
});

function nodeAt(x: number, y: number): CanvasNodeData {
    return { id: "node-a", type: CanvasNodeType.Text, title: "Node A", position: { x, y }, width: 200, height: 100 };
}

function renderNode(onPositionChange = vi.fn(), scale = 1) {
    return render(
        <DraggableCanvasNode node={nodeAt(10, 20)} scale={scale} onPositionChange={onPositionChange}>
            <span>node content</span>
        </DraggableCanvasNode>,
    );
}

it.each([
    [0.5, 100],
    [1, 50],
    [2, 25],
])("moves by screen delta divided by scale %s", (scale, expectedWorldDelta) => {
    const onPositionChange = vi.fn();
    render(
        <DraggableCanvasNode node={nodeAt(10, 20)} scale={scale} onPositionChange={onPositionChange}>
            <span>node content</span>
        </DraggableCanvasNode>,
    );

    fireEvent.pointerDown(screen.getByTestId("draggable-node-node-a"), { button: 0, pointerId: 1, clientX: 40, clientY: 50 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 90, clientY: 100 });
    fireEvent.pointerUp(window, { pointerId: 1 });

    expect(onPositionChange).toHaveBeenLastCalledWith("node-a", { x: 10 + expectedWorldDelta, y: 20 + expectedWorldDelta });
});

it("owns the initiating pointer and does not bubble its pointer down", () => {
    const onPositionChange = vi.fn();
    const onParentPointerDown = vi.fn();
    render(
        <div onPointerDown={onParentPointerDown}>
            <DraggableCanvasNode node={nodeAt(10, 20)} scale={1} onPositionChange={onPositionChange}>
                <span>node content</span>
            </DraggableCanvasNode>
        </div>,
    );

    const node = screen.getByTestId("draggable-node-node-a");
    const capture = vi.fn();
    Object.defineProperty(node, "setPointerCapture", { configurable: true, value: capture });
    fireEvent.pointerDown(node, { button: 0, pointerId: 7, clientX: 10, clientY: 20 });
    fireEvent.pointerMove(window, { pointerId: 8, clientX: 80, clientY: 90 });
    fireEvent.pointerUp(window, { pointerId: 8 });
    fireEvent.pointerCancel(window, { pointerId: 8 });

    expect(onParentPointerDown).not.toHaveBeenCalled();
    expect(capture).toHaveBeenCalledWith(7);
    expect(onPositionChange).not.toHaveBeenCalled();

    fireEvent.pointerMove(window, { pointerId: 7, clientX: 30, clientY: 50 });
    fireEvent.pointerUp(window, { pointerId: 7 });
    expect(onPositionChange).toHaveBeenLastCalledWith("node-a", { x: 30, y: 50 });
});

it("stops movement after pointer cancel and window blur", () => {
    const onPositionChange = vi.fn();
    renderNode(onPositionChange);
    const node = screen.getByTestId("draggable-node-node-a");

    fireEvent.pointerDown(node, { button: 0, pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerCancel(window, { pointerId: 1 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 20, clientY: 20 });
    expect(onPositionChange).toHaveBeenCalledTimes(1);

    fireEvent.pointerDown(node, { button: 0, pointerId: 2, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { pointerId: 2, clientX: 30, clientY: 30 });
    fireEvent.blur(window);
    fireEvent.pointerMove(window, { pointerId: 2, clientX: 40, clientY: 40 });
    expect(onPositionChange).toHaveBeenCalledTimes(2);
});

it("ignores non-left pointer buttons", () => {
    const onPositionChange = vi.fn();
    renderNode(onPositionChange);

    fireEvent.pointerDown(screen.getByTestId("draggable-node-node-a"), { button: 1, pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 50, clientY: 50 });
    fireEvent.pointerUp(window, { pointerId: 1 });

    expect(onPositionChange).not.toHaveBeenCalled();
});

it.each([
    ["Ctrl", { ctrlKey: true }],
    ["Cmd", { metaKey: true }],
])("reserves %s-left pointer down for selection and lets it propagate", (_name, modifier) => {
    const onPositionChange = vi.fn();
    const onParentPointerDown = vi.fn();
    render(
        <div onPointerDown={onParentPointerDown}>
            <DraggableCanvasNode node={nodeAt(10, 20)} scale={1} onPositionChange={onPositionChange}>
                <span>node content</span>
            </DraggableCanvasNode>
        </div>,
    );

    fireEvent.pointerDown(screen.getByTestId("draggable-node-node-a"), { button: 0, pointerId: 1, clientX: 0, clientY: 0, ...modifier });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 50, clientY: 50 });
    fireEvent.pointerUp(window, { pointerId: 1 });

    expect(onParentPointerDown).toHaveBeenCalledTimes(1);
    expect(onPositionChange).not.toHaveBeenCalled();
});

it("prevents native image behavior when an image surface starts a node drag", () => {
    const onPositionChange = vi.fn();
    render(
        <DraggableCanvasNode node={nodeAt(10, 20)} scale={1} onPositionChange={onPositionChange}>
            <img src="/api/v1/results/result-a" alt="generated result" />
        </DraggableCanvasNode>,
    );

    const eventAllowed = fireEvent.pointerDown(screen.getByRole("img", { name: "generated result" }), { button: 0, pointerId: 1, clientX: 10, clientY: 20 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 40, clientY: 60 });
    fireEvent.pointerUp(window, { pointerId: 1 });

    expect(eventAllowed).toBe(false);
    expect(onPositionChange).toHaveBeenLastCalledWith("node-a", { x: 40, y: 60 });
});

it.each([
    ["button", <button type="button">button</button>],
    ["input", <input aria-label="input" />],
    ["textarea", <textarea aria-label="textarea" />],
    ["select", <select aria-label="select"><option>option</option></select>],
    ["a", <a href="#target">link</a>],
    ["video", <video aria-label="video" />],
    ["audio", <audio aria-label="audio" />],
])("does not drag from an interactive %s descendant", (name, child) => {
    const onPositionChange = vi.fn();
    render(
        <DraggableCanvasNode node={nodeAt(10, 20)} scale={1} onPositionChange={onPositionChange}>
            <div data-testid={`interactive-${name}`}>{child}</div>
        </DraggableCanvasNode>,
    );

    fireEvent.pointerDown(screen.getByTestId(`interactive-${name}`).firstElementChild!, { button: 0, pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 50, clientY: 50 });
    fireEvent.pointerUp(window, { pointerId: 1 });

    expect(onPositionChange).not.toHaveBeenCalled();
});

it("coalesces pointer moves into one animation frame using the latest coordinates", () => {
    const frames: FrameRequestCallback[] = [];
    const requestFrame = vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
        frames.push(callback);
        return frames.length;
    });
    const onPositionChange = vi.fn();
    renderNode(onPositionChange);

    fireEvent.pointerDown(screen.getByTestId("draggable-node-node-a"), { button: 0, pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 20, clientY: 30 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 60, clientY: 80 });

    expect(requestFrame).toHaveBeenCalledTimes(1);
    expect(onPositionChange).not.toHaveBeenCalled();
    frames[0](0);
    expect(onPositionChange).toHaveBeenCalledTimes(1);
    expect(onPositionChange).toHaveBeenLastCalledWith("node-a", { x: 60, y: 90 });

    fireEvent.pointerUp(window, { pointerId: 1 });
    expect(onPositionChange).toHaveBeenCalledTimes(1);
});

it("restores the prior body cursor on pointer up, cancel, blur, and unmount", () => {
    document.body.style.cursor = "crosshair";
    const view = renderNode();
    const node = screen.getByTestId("draggable-node-node-a");

    fireEvent.pointerDown(node, { button: 0, pointerId: 1, clientX: 0, clientY: 0 });
    expect(document.body.style.cursor).toBe("grabbing");
    fireEvent.pointerUp(window, { pointerId: 1 });
    expect(document.body.style.cursor).toBe("crosshair");

    fireEvent.pointerDown(node, { button: 0, pointerId: 2, clientX: 0, clientY: 0 });
    fireEvent.pointerCancel(window, { pointerId: 2 });
    expect(document.body.style.cursor).toBe("crosshair");

    fireEvent.pointerDown(node, { button: 0, pointerId: 3, clientX: 0, clientY: 0 });
    fireEvent.blur(window);
    expect(document.body.style.cursor).toBe("crosshair");

    fireEvent.pointerDown(node, { button: 0, pointerId: 4, clientX: 0, clientY: 0 });
    view.unmount();
    expect(document.body.style.cursor).toBe("crosshair");
});

it("registers global drag listeners only while a node owns an active pointer", () => {
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    render(
        <>
            <DraggableCanvasNode node={nodeAt(10, 20)} scale={1} onPositionChange={vi.fn()}><span>first</span></DraggableCanvasNode>
            <DraggableCanvasNode node={{ ...nodeAt(30, 40), id: "node-b" }} scale={1} onPositionChange={vi.fn()}><span>second</span></DraggableCanvasNode>
        </>,
    );
    const dragEvents = new Set(["pointermove", "pointerup", "pointercancel", "blur"]);
    const addedDragEvents = () => add.mock.calls.filter(([name]) => dragEvents.has(String(name)));
    const removedDragEvents = () => remove.mock.calls.filter(([name]) => dragEvents.has(String(name)));

    expect(addedDragEvents()).toHaveLength(0);
    fireEvent.pointerDown(screen.getByTestId("draggable-node-node-a"), { button: 0, pointerId: 7, clientX: 10, clientY: 20 });
    expect(addedDragEvents().map(([name]) => name)).toEqual(["pointermove", "pointerup", "pointercancel", "blur"]);

    fireEvent.pointerUp(window, { pointerId: 7 });
    expect(removedDragEvents().map(([name]) => name)).toEqual(["pointermove", "pointerup", "pointercancel", "blur"]);
});
