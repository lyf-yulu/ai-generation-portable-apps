import { useEffect, useRef, useState, type FocusEvent as ReactFocusEvent, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";

import { formatNodeScale, nextNodeScale, nodeScaleOf } from "@/lib/canvas/node-scale";
import type { CanvasNodeData, Position } from "@/types/canvas";

type DraggableCanvasNodeProps = {
    node: CanvasNodeData;
    scale: number;
    onPositionChange: (nodeId: string, position: Position) => void;
    onMeasuredSize?: (nodeId: string, size: { width: number; height: number }) => void;
    onScaleChange?: (nodeId: string, scale: number) => void;
    selected?: boolean;
    disabled?: boolean;
    contentSized?: boolean;
    onSelect?: (nodeId: string, additive: boolean) => void;
    onContextMenu?: (nodeId: string, position: { x: number; y: number }, trigger: HTMLDivElement) => void;
    children: ReactNode;
    /** Rendered outside the scaled content wrapper so port stacking is not trapped by the wrapper's transform. */
    overlays?: ReactNode;
};

type DragState = {
    active: boolean;
    pointerId: number | null;
    startX: number;
    startY: number;
    initial: Position;
    scale: number;
    previousCursor: string;
};

type InteractivePointerGesture = {
    pointerId: number;
    target: Element;
};

const interactiveSelector = "button,input,textarea,select,label,a,video,audio,[contenteditable]:not([contenteditable='false']),[data-canvas-no-drag]";

function normalizedScale(scale: number) {
    return Number.isFinite(scale) && scale > 0 ? scale : 1;
}

export function DraggableCanvasNode({ node, scale, onPositionChange, onMeasuredSize, onScaleChange, selected = false, disabled = false, contentSized = false, onSelect, onContextMenu, children, overlays }: DraggableCanvasNodeProps) {
    const elementRef = useRef<HTMLDivElement>(null);
    const contentRef = useRef<HTMLDivElement>(null);
    const dragRef = useRef<DragState>({
        active: false,
        pointerId: null,
        startX: 0,
        startY: 0,
        initial: node.position,
        scale: 1,
        previousCursor: "",
    });
    const frameRef = useRef<number | null>(null);
    const nextPositionRef = useRef<Position | null>(null);
    const onPositionChangeRef = useRef(onPositionChange);
    const nodeIdRef = useRef(node.id);
    const finishDragRef = useRef<((pointerId?: number, flush?: boolean) => void) | null>(null);
    const interactivePointerGestureRef = useRef<InteractivePointerGesture | null>(null);
    const clearInteractivePointerRef = useRef<() => void>(() => undefined);
    const nodeScale = nodeScaleOf(node);
    const [contentSize, setContentSize] = useState<{ width: number; height: number } | null>(null);
    onPositionChangeRef.current = onPositionChange;
    nodeIdRef.current = node.id;

    useEffect(() => {
        return () => {
            finishDragRef.current?.(undefined, false);
            clearInteractivePointerRef.current();
        };
    }, []);

    useEffect(() => {
        const element = contentRef.current;
        if (!element || !onMeasuredSize || typeof ResizeObserver === "undefined") return;
        let active = true;
        const observer = new ResizeObserver((entries) => {
            if (!active) return;
            const entry = entries[0];
            if (!entry) return;
            const width = entry.contentRect.width;
            const height = entry.contentRect.height;
            if (Number.isFinite(width) && width > 0 && Number.isFinite(height) && height > 0) {
                setContentSize({ width, height });
                onMeasuredSize(node.id, { width: width * nodeScale, height: height * nodeScale });
            }
        });
        observer.observe(element);
        return () => {
            active = false;
            observer.disconnect();
        };
    }, [node.id, nodeScale, onMeasuredSize]);

    const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
        if (disabled || event.button !== 0 || dragRef.current.active) return;
        const target = event.target instanceof Element ? event.target : null;
        if (target?.closest(interactiveSelector)) return;
        onSelect?.(node.id, event.ctrlKey || event.metaKey || event.shiftKey);
        if (event.ctrlKey || event.metaKey || event.shiftKey) return;

        event.preventDefault();
        event.stopPropagation();
        event.currentTarget.setPointerCapture?.(event.pointerId);
        dragRef.current = {
            active: true,
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            initial: node.position,
            scale: normalizedScale(scale),
            previousCursor: document.body.style.cursor,
        };
        document.body.style.cursor = "grabbing";

        const emitPendingPosition = () => {
            const nextPosition = nextPositionRef.current;
            nextPositionRef.current = null;
            if (nextPosition) onPositionChangeRef.current(nodeIdRef.current, nextPosition);
        };

        let listening = true;
        const detachListeners = () => {
            if (!listening) return;
            listening = false;
            window.removeEventListener("pointermove", handlePointerMove);
            window.removeEventListener("pointerup", handlePointerUp);
            window.removeEventListener("pointercancel", handlePointerCancel);
            window.removeEventListener("blur", handleWindowBlur);
        };
        const finishDrag = (pointerId?: number, flush = true) => {
            const drag = dragRef.current;
            if (!drag.active || (pointerId !== undefined && pointerId !== drag.pointerId)) return;

            if (frameRef.current !== null) {
                cancelAnimationFrame(frameRef.current);
                frameRef.current = null;
            }
            drag.active = false;
            drag.pointerId = null;
            document.body.style.cursor = drag.previousCursor;
            detachListeners();
            finishDragRef.current = null;
            if (flush) emitPendingPosition();
            else nextPositionRef.current = null;
        };

        const handlePointerMove = (event: PointerEvent) => {
            const drag = dragRef.current;
            if (!drag.active || event.pointerId !== drag.pointerId) return;

            const position = {
                x: drag.initial.x + (event.clientX - drag.startX) / drag.scale,
                y: drag.initial.y + (event.clientY - drag.startY) / drag.scale,
            };
            if (!Number.isFinite(position.x) || !Number.isFinite(position.y)) return;

            nextPositionRef.current = position;
            if (frameRef.current !== null) return;
            frameRef.current = requestAnimationFrame(() => {
                frameRef.current = null;
                emitPendingPosition();
            });
        };

        const handlePointerUp = (event: PointerEvent) => finishDrag(event.pointerId);
        const handlePointerCancel = (event: PointerEvent) => finishDrag(event.pointerId);
        const handleWindowBlur = () => finishDrag();

        window.addEventListener("pointermove", handlePointerMove);
        window.addEventListener("pointerup", handlePointerUp);
        window.addEventListener("pointercancel", handlePointerCancel);
        window.addEventListener("blur", handleWindowBlur);
        finishDragRef.current = finishDrag;
    };

    const handlePointerDownCapture = (event: ReactPointerEvent<HTMLDivElement>) => {
        if (disabled || event.button !== 0) return;
        const target = event.target instanceof Element ? event.target : null;
        const interactiveTarget = target?.closest(interactiveSelector) ?? null;
        if (!interactiveTarget) return;
        clearInteractivePointerRef.current();
        const gesture = { pointerId: event.pointerId, target: interactiveTarget };
        interactivePointerGestureRef.current = gesture;
        let expiryTimer: number | null = null;
        const clear = () => {
            if (interactivePointerGestureRef.current === gesture) interactivePointerGestureRef.current = null;
            window.removeEventListener("pointerup", finish);
            window.removeEventListener("pointercancel", finish);
            window.removeEventListener("blur", clear);
            if (expiryTimer !== null) window.clearTimeout(expiryTimer);
            if (clearInteractivePointerRef.current === clear) clearInteractivePointerRef.current = () => undefined;
        };
        const finish = (pointerEvent: PointerEvent) => {
            if (pointerEvent.pointerId === gesture.pointerId) clear();
        };
        window.addEventListener("pointerup", finish);
        window.addEventListener("pointercancel", finish);
        window.addEventListener("blur", clear);
        expiryTimer = window.setTimeout(clear, 0);
        clearInteractivePointerRef.current = clear;
        onSelect?.(node.id, event.ctrlKey || event.metaKey || event.shiftKey);
    };

    const handleFocusCapture = (event: ReactFocusEvent<HTMLDivElement>) => {
        if (disabled) return;
        const target = event.target instanceof Element ? event.target : null;
        if (!target?.closest(interactiveSelector)) return;
        const gesture = interactivePointerGestureRef.current;
        clearInteractivePointerRef.current();
        if (gesture && (gesture.target === target || gesture.target.contains(target))) return;
        if (!selected) onSelect?.(node.id, false);
    };

    const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
        if (disabled || event.target !== event.currentTarget) return;
        if (event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey)) {
            if (!onContextMenu) return;
            event.preventDefault();
            event.stopPropagation();
            const rect = event.currentTarget.getBoundingClientRect();
            onContextMenu(node.id, { x: rect.left + Math.min(rect.width, 24), y: rect.top + Math.min(rect.height, 24) }, event.currentTarget);
            return;
        }
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        onSelect?.(node.id, event.ctrlKey || event.metaKey || event.shiftKey);
    };

    const boxWidth = (contentSize?.width ?? node.width) * nodeScale;
    const boxHeight = contentSized ? (contentSize ? contentSize.height * nodeScale : undefined) : (contentSize?.height ?? node.height) * nodeScale;
    const stepScale = (direction: 1 | -1) => {
        const next = nextNodeScale(nodeScale, direction);
        if (next !== nodeScale) onScaleChange?.(node.id, next);
    };

    return (
        <div
            ref={elementRef}
            data-node-id={node.id}
            data-testid={`draggable-node-${node.id}`}
            role="option"
            aria-label={node.title}
            aria-selected={selected}
            aria-disabled={disabled || undefined}
            tabIndex={0}
            className={`absolute rounded-xl outline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#5b8ff0] ${selected ? "outline outline-2 outline-[#235fd6] shadow-[0_0_0_4px_rgba(88,237,135,0.18)]" : ""}`}
            style={{ left: node.position.x, top: node.position.y, width: boxWidth, ...(boxHeight === undefined ? {} : { height: boxHeight }) }}
            onPointerDownCapture={handlePointerDownCapture}
            onPointerDown={handlePointerDown}
            onFocusCapture={handleFocusCapture}
            onKeyDown={handleKeyDown}
            onContextMenu={(event) => {
                const target = event.target instanceof Element ? event.target : null;
                if (disabled || target?.closest(interactiveSelector) || !onContextMenu) return;
                event.preventDefault();
                event.stopPropagation();
                onContextMenu(node.id, { x: event.clientX, y: event.clientY }, event.currentTarget);
            }}
        >
            <div
                ref={contentRef}
                data-testid={`node-content-${node.id}`}
                className="origin-top-left"
                style={{ width: node.width, minHeight: contentSized ? undefined : node.height, transform: `scale(${nodeScale})`, transformOrigin: "top left" }}
            >
                {children}
            </div>
            {overlays}
            {selected && !disabled && onScaleChange ? (
                <div data-canvas-no-drag data-canvas-no-zoom className="absolute top-2 left-2 z-10 flex items-center gap-0.5 rounded-lg border border-[#c3ccd9] bg-[#ffffff]/95 px-0.5 py-0.5">
                    <button type="button" aria-label="缩小节点" title="缩小节点" onClick={() => stepScale(-1)} className="rounded px-1.5 text-sm leading-5 text-[#465267] hover:bg-[#eef2f7]">−</button>
                    <button type="button" aria-label="重置节点缩放" title="重置为真实大小" onClick={() => onScaleChange(node.id, 1)} className="min-w-10 rounded px-1 text-xs leading-5 text-[#235fd6] hover:bg-[#eef2f7]">{formatNodeScale(nodeScale)}</button>
                    <button type="button" aria-label="放大节点" title="放大节点" onClick={() => stepScale(1)} className="rounded px-1.5 text-sm leading-5 text-[#465267] hover:bg-[#eef2f7]">+</button>
                </div>
            ) : null}
        </div>
    );
}
