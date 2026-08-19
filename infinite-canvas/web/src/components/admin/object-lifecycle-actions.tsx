import { useEffect, useRef, useState } from "react";

import { ApiRequestError } from "@/api/client";

type MaybePromise<T> = T | Promise<T>;
type Props<T = unknown> = {
    objectIdentity?: string; objectLabel: string; enabled: boolean; archivedAt?: string | null; revision: number; historical?: boolean;
    onEnable: (revision: number) => MaybePromise<T>; onDisable: (revision: number) => MaybePromise<T>;
    onArchive: (revision: number) => MaybePromise<T>; onRestore: (revision: number) => MaybePromise<T>;
    onDelete: (revision: number) => MaybePromise<void>; onPurge: (revision: number) => MaybePromise<T>;
    onChanged: (updated: T) => void; onDeleted?: () => void; onRefresh?: () => void;
};

const referenceLabels = { job: "任务", access: "访问授权", assignment: "账号派发", route: "线路", model: "模型" } as const;

export function ObjectLifecycleActions<T>({ objectIdentity, objectLabel, enabled, archivedAt = null, revision, historical = false, onEnable, onDisable, onArchive, onRestore, onDelete, onPurge, onChanged, onDeleted, onRefresh }: Props<T>) {
    const identity = objectIdentity || objectLabel;
    const [busy, setBusy] = useState(false);
    const [confirm, setConfirm] = useState<"delete" | "purge" | null>(null);
    const [typed, setTyped] = useState("");
    const [error, setError] = useState<ApiRequestError | null>(null);
    const confirmInput = useRef<HTMLInputElement>(null);
    const priorFocus = useRef<HTMLElement | null>(null);
    const identityGeneration = useRef(0);
    const busyRef = useRef(false);
    useEffect(() => {
        identityGeneration.current += 1;
        busyRef.current = false; setBusy(false); setConfirm(null); setTyped(""); setError(null); priorFocus.current = null;
        return () => { identityGeneration.current += 1; };
    }, [identity, objectLabel, revision]);
    useEffect(() => { if (confirm) confirmInput.current?.focus(); }, [confirm]);
    const close = () => { setConfirm(null); setTyped(""); queueMicrotask(() => priorFocus.current?.focus()); };
    const open = (kind: "delete" | "purge") => { priorFocus.current = document.activeElement as HTMLElement | null; setError(null); setConfirm(kind); };
    const perform = async (action: (value: number) => MaybePromise<T>) => {
        if (busyRef.current) return;
        const generation = identityGeneration.current;
        busyRef.current = true;
        setBusy(true); setError(null);
        try { const updated = await action(revision); if (identityGeneration.current === generation) onChanged(updated); } catch (caught) { if (identityGeneration.current === generation) setError(caught instanceof ApiRequestError ? caught : new ApiRequestError({ code: "request_failed", message: "", retryable: false, request_id: "", phase: "response" })); } finally { if (identityGeneration.current === generation) { busyRef.current = false; setBusy(false); } }
    };
    const destructive = async () => {
        if (busyRef.current || typed !== objectLabel || !confirm) return;
        const generation = identityGeneration.current;
        busyRef.current = true;
        setBusy(true); setError(null);
        try {
            if (confirm === "delete") { await onDelete(revision); if (identityGeneration.current === generation) { close(); onDeleted?.(); } }
            else { const updated = await onPurge(revision); if (identityGeneration.current === generation) { close(); onChanged(updated); } }
        } catch (caught) { if (identityGeneration.current === generation) setError(caught instanceof ApiRequestError ? caught : new ApiRequestError({ code: "request_failed", message: "", retryable: false, request_id: "", phase: "response" })); }
        finally { if (identityGeneration.current === generation) { busyRef.current = false; setBusy(false); } }
    };
    if (historical) return <p className="mt-4 border-t border-[#594d2a] pt-4 text-sm text-[#cdbf83]">审计记录永久保留，仅供只读追溯。</p>;
    return <div className="mt-4 border-t border-[#1e482b] pt-4">
        <div className="flex flex-wrap gap-2">
            {!historical && (archivedAt ? <button type="button" disabled={busy} onClick={() => void perform(onRestore)} className="rounded border border-[#3a7650] px-3 py-1.5 text-sm text-[#8ff0aa] disabled:opacity-50">恢复</button> : <>
                {enabled ? <button type="button" disabled={busy} onClick={() => void perform(onDisable)} className="rounded border border-[#3a7650] px-3 py-1.5 text-sm text-[#d0e8d6] disabled:opacity-50">停用</button> : <button type="button" disabled={busy} onClick={() => void perform(onEnable)} className="rounded border border-[#3a7650] px-3 py-1.5 text-sm text-[#8ff0aa] disabled:opacity-50">启用</button>}
                <button type="button" disabled={busy} onClick={() => void perform(onArchive)} className="rounded border border-[#6c6131] px-3 py-1.5 text-sm text-[#eadc91] disabled:opacity-50">归档</button>
            </>)}
            <button type="button" disabled={busy} onClick={() => open("delete")} className="rounded border border-[#78433d] px-3 py-1.5 text-sm text-[#ffb4a8] disabled:opacity-50">删除</button>
            {!historical && <button type="button" disabled={busy} onClick={() => open("purge")} className="rounded border border-[#78433d] px-3 py-1.5 text-sm text-[#ffb4a8] disabled:opacity-50">清理历史运行配置</button>}
        </div>
        {error?.code === "REVISION_CONFLICT" && <div role="alert" className="mt-3 flex flex-wrap gap-2 text-sm text-[#ffbd73]">配置已变化，请重新加载。{onRefresh && <button type="button" onClick={onRefresh} className="underline">重新加载</button>}</div>}
        {error?.code === "RESOURCE_REFERENCED" && <div role="alert" className="mt-3 text-sm text-[#ffbd73]">对象仍被引用：{Object.entries(error.references || {}).map(([key, count]) => `${referenceLabels[key as keyof typeof referenceLabels]} ${count}`).join("，") || "引用状态不可用"}。可保留审计记录并清理运行配置。</div>}
        {error && !["REVISION_CONFLICT", "RESOURCE_REFERENCED"].includes(error.code) && <p role="alert" className="mt-3 text-sm text-[#ffbd73]">操作未完成，请重试。</p>}
        {confirm && <div role="dialog" aria-modal="true" aria-labelledby="object-confirm-title" onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); close(); } }} className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4">
            <div className="w-full max-w-md rounded-xl border border-[#3a7650] bg-[#07110b] p-5 shadow-2xl">
                <h2 id="object-confirm-title" className="text-lg font-semibold text-[#e5f5e9]">{confirm === "delete" ? "确认删除" : "确认清理历史运行配置"}</h2>
                <p className="mt-2 text-sm text-[#a9c6b0]">请输入 <strong className="text-[#e5f5e9]">{objectLabel}</strong>。该操作不会显示或保留任何部署凭据。</p>
                <label className="mt-4 block text-sm text-[#c9decf]">输入 {objectLabel} 确认{confirm === "delete" ? "删除" : "清理"}<input ref={confirmInput} aria-label={`输入 ${objectLabel} 确认${confirm === "delete" ? "删除" : "清理"}`} value={typed} onChange={(event) => setTyped(event.target.value)} className="mt-1 block w-full min-w-0 rounded border border-[#3a7650] bg-[#0b1710] px-3 py-2 text-[#e5f5e9]" /></label>
                <div className="mt-4 flex justify-end gap-2"><button type="button" onClick={close} className="rounded border border-[#3a7650] px-3 py-2 text-sm text-[#c9decf]">取消</button><button type="button" disabled={busy || typed !== objectLabel} onClick={() => void destructive()} className="rounded bg-[#a94235] px-3 py-2 text-sm font-medium text-white disabled:opacity-40">{confirm === "delete" ? "确认删除" : "确认清理"}</button></div>
            </div>
        </div>}
    </div>;
}
