import React, { useRef, useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { InfiniteCanvas } from "@/components/canvas/infinite-canvas";
import { normalizeViewport, zoomViewportAt } from "@/features/canvas/viewport";
import type { ViewportTransform } from "@/types/canvas";

afterEach(cleanup);

function CanvasHarness({ children }: { children?: React.ReactNode }) {
    const containerRef = useRef<HTMLDivElement>(null);
    const [viewport, setViewport] = useState<ViewportTransform>({ x: 0, y: 0, k: 1 });
    return (
        <>
            <output data-testid="viewport">{`${viewport.x},${viewport.y},${viewport.k}`}</output>
            <InfiniteCanvas containerRef={containerRef} viewport={viewport} onViewportChange={setViewport}>
                {children}
            </InfiniteCanvas>
        </>
    );
}

function RerenderingPanHarness() {
    const containerRef = useRef<HTMLDivElement>(null);
    const [viewport, setViewport] = useState<ViewportTransform>({ x: 0, y: 0, k: 1 });
    return (
        <>
            <output data-testid="viewport">{`${viewport.x},${viewport.y},${viewport.k}`}</output>
            <InfiniteCanvas containerRef={containerRef} viewport={viewport} onViewportChange={(next) => setViewport(next)} onCanvasDeselect={() => undefined}>
                <div />
            </InfiniteCanvas>
        </>
    );
}

it("keeps the world point under the pointer fixed while zooming", () => {
    const next = zoomViewportAt(
        { x: 20, y: 30, k: 1 },
        { x: 200, y: 150 },
        -100,
    );
    expect((200 - next.x) / next.k).toBeCloseTo(180);
    expect((150 - next.y) / next.k).toBeCloseTo(120);
});

it("normalizes legacy and hostile viewport values", () => {
    expect(normalizeViewport({ x: Number.NaN, y: 2, k: 99 })).toEqual({ x: 0, y: 0, k: 1 });
    expect(normalizeViewport({ x: 4, y: 5, k: 0.001 })).toEqual({ x: 4, y: 5, k: 0.05 });
    expect(normalizeViewport({ x: 4, y: 5, k: 9 })).toEqual({ x: 4, y: 5, k: 5 });
});

it("pans on blank left drag and does not pan from a node", () => {
    render(<CanvasHarness><div data-node-id="node-a">node</div></CanvasHarness>);
    const canvas = screen.getByTestId("infinite-canvas");
    fireEvent.pointerDown(canvas, { button: 0, clientX: 20, clientY: 30, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 80, clientY: 90, pointerId: 1 });
    fireEvent.pointerUp(window, { pointerId: 1 });
    expect(screen.getByTestId("viewport")).toHaveTextContent("60,60,1");

    fireEvent.pointerDown(screen.getByText("node"), { button: 0, clientX: 80, clientY: 90, pointerId: 2 });
    fireEvent.pointerMove(window, { clientX: 140, clientY: 150, pointerId: 2 });
    fireEvent.pointerUp(window, { pointerId: 2 });
    expect(screen.getByTestId("viewport")).toHaveTextContent("60,60,1");
});

it("releases an editor focus when blank-canvas interaction begins", () => {
    render(<CanvasHarness><div data-node-id="node-a"><textarea aria-label="node editor" /></div></CanvasHarness>);
    const editor = screen.getByLabelText("node editor");
    const canvas = screen.getByTestId("infinite-canvas");
    editor.focus();
    expect(editor).toHaveFocus();

    fireEvent.pointerDown(canvas, { button: 0, clientX: 20, clientY: 30, pointerId: 9 });
    expect(canvas).toHaveFocus();
    fireEvent.pointerUp(window, { pointerId: 9 });

    editor.focus();
    fireEvent.pointerDown(editor, { button: 0, clientX: 25, clientY: 35, pointerId: 10 });
    expect(editor).toHaveFocus();
});

it("keeps a pan owned by its initiating pointer", () => {
    render(<CanvasHarness />);
    const canvas = screen.getByTestId("infinite-canvas");

    fireEvent.pointerDown(canvas, { button: 0, clientX: 20, clientY: 30, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 120, clientY: 130, pointerId: 2 });
    fireEvent.pointerUp(window, { pointerId: 2 });
    fireEvent.pointerCancel(window, { pointerId: 2 });
    expect(screen.getByTestId("viewport")).toHaveTextContent("0,0,1");

    fireEvent.pointerMove(window, { clientX: 30, clientY: 50, pointerId: 1 });
    fireEvent.pointerUp(window, { pointerId: 1 });
    expect(screen.getByTestId("viewport")).toHaveTextContent("10,20,1");
});

it("does not replay an already applied pan on pointer up", async () => {
    const containerRef = React.createRef<HTMLDivElement>();
    const onViewportChange = vi.fn();
    render(
        <InfiniteCanvas containerRef={containerRef} viewport={{ x: 0, y: 0, k: 1 }} onViewportChange={onViewportChange}>
            <div />
        </InfiniteCanvas>,
    );
    const canvas = screen.getByTestId("infinite-canvas");

    fireEvent.pointerDown(canvas, { button: 0, clientX: 20, clientY: 30, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 80, clientY: 90, pointerId: 1 });
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    fireEvent.pointerUp(window, { pointerId: 1 });

    expect(onViewportChange).toHaveBeenCalledTimes(1);
});

it("continues panning after viewport updates rerender the parent with new callback identities", async () => {
    render(<RerenderingPanHarness />);
    const canvas = screen.getByTestId("infinite-canvas");

    fireEvent.pointerDown(canvas, { button: 0, clientX: 20, clientY: 30, pointerId: 7 });
    fireEvent.pointerMove(window, { clientX: 60, clientY: 60, pointerId: 7 });
    await waitFor(() => expect(screen.getByTestId("viewport")).toHaveTextContent("40,30,1"));

    fireEvent.pointerMove(window, { clientX: 160, clientY: 180, pointerId: 7 });
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    fireEvent.pointerUp(window, { pointerId: 7 });

    await waitFor(() => expect(screen.getByTestId("viewport")).toHaveTextContent("140,150,1"));
});

it("does not zoom from excluded controls and delegates ordinary wheel zoom", () => {
    render(<CanvasHarness><button data-canvas-no-zoom>control</button></CanvasHarness>);
    const canvas = screen.getByTestId("infinite-canvas");

    fireEvent.wheel(screen.getByRole("button", { name: "control" }), { clientX: 200, clientY: 150, deltaY: -100 });
    expect(screen.getByTestId("viewport")).toHaveTextContent("0,0,1");

    fireEvent.wheel(canvas, { clientX: 200, clientY: 150, deltaY: -100 });
    const expected = zoomViewportAt({ x: 0, y: 0, k: 1 }, { x: 200, y: 150 }, -100);
    expect(screen.getByTestId("viewport")).toHaveTextContent(`${expected.x},${expected.y},${expected.k}`);
});

it("restores the prior global cursor when unmounted during an active pan", () => {
    document.body.style.cursor = "crosshair";
    const view = render(<CanvasHarness />);
    fireEvent.pointerDown(screen.getByTestId("infinite-canvas"), { button: 0, clientX: 20, clientY: 30, pointerId: 1 });
    expect(document.body.style.cursor).toBe("grabbing");

    view.unmount();

    expect(document.body.style.cursor).toBe("crosshair");
    fireEvent.pointerMove(window, { clientX: 80, clientY: 90, pointerId: 1 });
    document.body.style.cursor = "";
});
