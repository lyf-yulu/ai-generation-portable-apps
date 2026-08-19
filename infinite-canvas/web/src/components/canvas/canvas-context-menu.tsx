import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Copy, Pencil, Scissors, Trash2 } from "lucide-react";

import { canvasThemes } from "@/lib/canvas-theme";
import { useThemeStore } from "@/stores/use-theme-store";
import type { ContextMenuState } from "@/types/canvas";

export function CanvasNodeContextMenu({ menu, onClose, onCopy, onCut, onRename, onDuplicate, onDelete }: { menu: ContextMenuState; onClose: (restoreFocus?: boolean) => void; onCopy?: () => void; onCut?: () => void; onRename?: () => void; onDuplicate?: () => void; onDelete: () => void }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const menuRef = useRef<HTMLDivElement>(null);
    const [position, setPosition] = useState({ left: menu.x, top: menu.y });

    const updatePosition = useCallback(() => {
        const rect = menuRef.current?.getBoundingClientRect();
        const menuWidth = rect?.width || 176;
        const menuHeight = rect?.height || 88;
        const viewport = window.visualViewport;
        const viewportLeft = viewport?.offsetLeft ?? 0;
        const viewportTop = viewport?.offsetTop ?? 0;
        const viewportWidth = viewport?.width ?? window.innerWidth;
        const viewportHeight = viewport?.height ?? window.innerHeight;
        const minLeft = viewportLeft + 8;
        const minTop = viewportTop + 8;
        const maxLeft = Math.max(minLeft, viewportLeft + viewportWidth - menuWidth - 8);
        const maxTop = Math.max(minTop, viewportTop + viewportHeight - menuHeight - 8);
        const next = {
            left: Math.max(minLeft, Math.min(menu.x, maxLeft)),
            top: Math.max(minTop, Math.min(menu.y, maxTop)),
        };
        setPosition((current) => current.left === next.left && current.top === next.top ? current : next);
    }, [menu.x, menu.y]);

    useLayoutEffect(() => {
        updatePosition();
        const viewport = window.visualViewport;
        const resizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(updatePosition);
        if (menuRef.current) resizeObserver?.observe(menuRef.current);
        window.addEventListener("resize", updatePosition);
        viewport?.addEventListener("resize", updatePosition);
        viewport?.addEventListener("scroll", updatePosition);
        return () => {
            window.removeEventListener("resize", updatePosition);
            viewport?.removeEventListener("resize", updatePosition);
            viewport?.removeEventListener("scroll", updatePosition);
            resizeObserver?.disconnect();
        };
    }, [updatePosition]);

    useEffect(() => {
        const close = (event: PointerEvent) => {
            const target = event.target;
            if (target instanceof Node && menuRef.current?.contains(target)) return;
            if (target instanceof Element && target.closest(".ant-popover")) return;
            onClose(false);
        };
        window.addEventListener("pointerdown", close);
        return () => window.removeEventListener("pointerdown", close);
    }, [onClose]);

    useEffect(() => {
        menuRef.current?.querySelector<HTMLElement>("[role='menuitem']")?.focus();
    }, [menu]);

    const moveFocus = (direction: 1 | -1) => {
        const items = [...(menuRef.current?.querySelectorAll<HTMLElement>("[role='menuitem']") ?? [])];
        if (!items.length) return;
        const current = items.indexOf(document.activeElement as HTMLElement);
        items[(current + direction + items.length) % items.length]?.focus();
    };

    const copyAction = onCopy ?? onDuplicate;

    return (
        <div
            ref={menuRef}
            role="menu"
            aria-label={menu.type === "node" ? "节点操作" : "连接操作"}
            className="fixed z-[80] min-w-44 overflow-hidden rounded-xl border py-1 shadow-2xl"
            style={{ left: position.left, top: position.top, background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.node.text }}
            onPointerDown={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
                if (event.key === "Escape") {
                    event.preventDefault();
                    event.stopPropagation();
                    onClose(true);
                } else if (event.key === "Tab") {
                    onClose(false);
                } else if (event.key === "ArrowDown") {
                    event.preventDefault();
                    moveFocus(1);
                } else if (event.key === "ArrowUp") {
                    event.preventDefault();
                    moveFocus(-1);
                }
            }}
        >
            {menu.type === "node" && copyAction ? <MenuButton icon={<Copy className="size-4" />} label="复制" onClick={copyAction} /> : null}
            {menu.type === "node" && onCut ? <MenuButton icon={<Scissors className="size-4" />} label="剪切" onClick={onCut} /> : null}
            {menu.type === "node" && onRename ? <MenuButton icon={<Pencil className="size-4" />} label="重命名" onClick={onRename} /> : null}
            <MenuButton icon={<Trash2 className="size-4" />} label="删除" onClick={onDelete} danger />
        </div>
    );
}

function MenuButton({ icon, label, onClick, danger = false }: { icon: ReactNode; label: string; onClick?: () => void; danger?: boolean }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];

    return (
        <button role="menuitem" type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:opacity-80" style={{ color: danger ? "#f87171" : theme.node.text }} onClick={onClick}>
            {icon}
            <span>{label}</span>
        </button>
    );
}
