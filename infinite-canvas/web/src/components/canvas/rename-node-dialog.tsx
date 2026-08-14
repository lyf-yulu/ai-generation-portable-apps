import { useEffect, useRef, useState } from "react";

import type { CanvasNodeData } from "@/types/canvas";

export function RenameNodeDialog({ node, onClose, onSave }: { node: CanvasNodeData; onClose: () => void; onSave: (title: string) => void }) {
    const [title, setTitle] = useState(node.title);
    const inputRef = useRef<HTMLInputElement>(null);
    const normalized = title.trim();

    useEffect(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
    }, []);

    const save = () => {
        if (!normalized) return;
        onSave(normalized.slice(0, 100));
    };

    return (
        <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/55 p-4" onPointerDown={(event) => event.target === event.currentTarget && onClose()}>
            <section role="dialog" aria-modal="true" aria-labelledby="rename-node-title" className="w-full max-w-sm rounded-xl border border-[#285038] bg-[#08100b] p-4 text-[#dceee1] shadow-2xl">
                <h2 id="rename-node-title" className="text-sm font-semibold">重命名节点</h2>
                <label className="mt-3 block text-xs text-[#9fb5a5]">
                    节点名称
                    <input ref={inputRef} aria-label="节点名称" maxLength={100} value={title} onChange={(event) => setTitle(event.target.value)} onKeyDown={(event) => {
                        if (event.key === "Enter") { event.preventDefault(); save(); }
                        else if (event.key === "Escape") { event.preventDefault(); onClose(); }
                    }} className="mt-1 block w-full rounded-lg border border-[#356b48] bg-[#050806] px-3 py-2 text-sm outline-none focus:border-[#58ed87]" />
                </label>
                <div className="mt-4 flex justify-end gap-2">
                    <button type="button" onClick={onClose} className="rounded-lg border border-[#355f43] px-3 py-2 text-xs">取消</button>
                    <button type="button" aria-label="保存名称" disabled={!normalized} onClick={save} className="rounded-lg bg-[#47d978] px-3 py-2 text-xs font-semibold text-[#041008] disabled:opacity-40">保存</button>
                </div>
            </section>
        </div>
    );
}
