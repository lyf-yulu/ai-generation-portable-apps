import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Film, ImagePlus, MessageSquareText, Music2, Sparkles } from "lucide-react";
import type { ReactNode } from "react";

import type { ContextMenuState } from "@/types/canvas";

export type CanvasCreationKind = "prompt" | "image" | "video" | "audio" | "image-model" | "video-model";

type Item = { kind: CanvasCreationKind; label: string; icon: ReactNode; disabled?: boolean; reason?: string };

export function CanvasCreateContextMenu({ menu, imageModelDisabled, videoModelDisabled, onClose, onCreate }: {
    menu: Extract<ContextMenuState, { type: "canvas" }>;
    imageModelDisabled: boolean;
    videoModelDisabled: boolean;
    onClose: (restoreFocus?: boolean) => void;
    onCreate: (kind: CanvasCreationKind) => void;
}) {
    const menuRef = useRef<HTMLDivElement>(null);
    const [position, setPosition] = useState({ left: menu.x, top: menu.y });
    const items: Item[] = [
        { kind: "prompt", label: "提示词", icon: <MessageSquareText className="size-4" /> },
        { kind: "image", label: "参考图片", icon: <ImagePlus className="size-4" /> },
        { kind: "video", label: "参考视频", icon: <Film className="size-4" /> },
        { kind: "audio", label: "参考音频", icon: <Music2 className="size-4" /> },
        { kind: "image-model", label: "图片生成", icon: <Sparkles className="size-4" />, disabled: imageModelDisabled, reason: "管理员尚未派发图片模型" },
        { kind: "video-model", label: "视频生成", icon: <Sparkles className="size-4" />, disabled: videoModelDisabled, reason: "管理员尚未派发视频模型" },
    ];

    const updatePosition = useCallback(() => {
        const rect = menuRef.current?.getBoundingClientRect();
        const width = rect?.width || 208;
        const height = rect?.height || 268;
        const viewport = window.visualViewport;
        const left = viewport?.offsetLeft ?? 0;
        const top = viewport?.offsetTop ?? 0;
        const maxLeft = Math.max(left + 8, left + (viewport?.width ?? window.innerWidth) - width - 8);
        const maxTop = Math.max(top + 8, top + (viewport?.height ?? window.innerHeight) - height - 8);
        setPosition({ left: Math.max(left + 8, Math.min(menu.x, maxLeft)), top: Math.max(top + 8, Math.min(menu.y, maxTop)) });
    }, [menu.x, menu.y]);

    useLayoutEffect(() => {
        updatePosition();
        const visualViewport = window.visualViewport;
        const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(updatePosition);
        if (menuRef.current) observer?.observe(menuRef.current);
        window.addEventListener("resize", updatePosition);
        visualViewport?.addEventListener("resize", updatePosition);
        visualViewport?.addEventListener("scroll", updatePosition);
        return () => {
            observer?.disconnect();
            window.removeEventListener("resize", updatePosition);
            visualViewport?.removeEventListener("resize", updatePosition);
            visualViewport?.removeEventListener("scroll", updatePosition);
        };
    }, [updatePosition]);

    useEffect(() => {
        menuRef.current?.querySelector<HTMLButtonElement>("[role='menuitem']:not(:disabled)")?.focus();
        const closeOutside = (event: PointerEvent) => {
            if (event.target instanceof Node && menuRef.current?.contains(event.target)) return;
            onClose(false);
        };
        window.addEventListener("pointerdown", closeOutside);
        return () => window.removeEventListener("pointerdown", closeOutside);
    }, [menu, onClose]);

    const moveFocus = (direction: 1 | -1) => {
        const enabled = [...(menuRef.current?.querySelectorAll<HTMLButtonElement>("[role='menuitem']:not(:disabled)") ?? [])];
        if (!enabled.length) return;
        const index = enabled.indexOf(document.activeElement as HTMLButtonElement);
        enabled[(index + direction + enabled.length) % enabled.length]?.focus();
    };

    return (
        <div ref={menuRef} role="menu" aria-label="创建节点" data-canvas-no-zoom className="fixed z-[80] min-w-52 overflow-hidden rounded-xl border border-[#285038] bg-[#08100b] py-1 text-[#dceee1] shadow-2xl" style={{ left: position.left, top: position.top }} onPointerDown={(event) => event.stopPropagation()} onKeyDown={(event) => {
            if (event.key === "Escape") { event.preventDefault(); onClose(true); }
            else if (event.key === "Tab") onClose(false);
            else if (event.key === "ArrowDown") { event.preventDefault(); moveFocus(1); }
            else if (event.key === "ArrowUp") { event.preventDefault(); moveFocus(-1); }
        }}>
            <p className="px-3 py-2 text-[10px] tracking-[0.14em] text-[#6d8f77]">在此处创建</p>
            {items.map((item) => (
                <button key={item.kind} role="menuitem" type="button" disabled={item.disabled} title={item.disabled ? item.reason : undefined} className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-[#173321] disabled:cursor-not-allowed disabled:opacity-40" onClick={() => onCreate(item.kind)} onKeyDown={(event) => {
                    if ((event.key === "Enter" || event.key === " ") && !item.disabled) { event.preventDefault(); onCreate(item.kind); }
                }}>
                    <span className="text-[#58ed87]">{item.icon}</span>
                    <span>{item.label}</span>
                </button>
            ))}
        </div>
    );
}
