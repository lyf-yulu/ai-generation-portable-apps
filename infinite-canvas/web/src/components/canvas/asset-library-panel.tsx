import { useCallback, useEffect, useRef, useState } from "react";
import { nanoid } from "nanoid";

import { fetchAsset as fetchAssetApi, fetchLibraryAssets, uploadLibraryAsset } from "@/api/assets";
import type { AssetRef } from "@/api/contracts";
import type { GraphMediaItem } from "@/features/graph/contracts";


type LibraryTarget = { nodeId: string; label: string };

type Props = {
    targets: readonly LibraryTarget[];
    onClose: () => void;
    upload?: (file: File, onProgress: (percent: number) => void, signal?: AbortSignal) => Promise<AssetRef>;
    fetchAssets?: () => Promise<AssetRef[]>;
    fetchAsset?: (id: string) => Promise<AssetRef>;
    addToCollection?: (nodeId: string, items: GraphMediaItem[]) => void;
    pollIntervalMs?: number;
};

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

function isPortraitImage(file: File): boolean {
    return file.type.startsWith("image/") && (file.type === "image/png" || file.type === "image/jpeg" || file.type === "image/webp");
}

export function AssetLibraryPanel({
    targets,
    onClose,
    upload = uploadLibraryAsset,
    fetchAssets = fetchLibraryAssets,
    fetchAsset = fetchAssetApi,
    addToCollection = () => undefined,
    pollIntervalMs = 5000,
}: Props) {
    const [assets, setAssets] = useState<AssetRef[]>([]);
    const [targetId, setTargetId] = useState<string>(targets[0]?.nodeId ?? "");
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const refresh = useCallback(async () => {
        try {
            setError(null);
            setAssets(await fetchAssets());
        } catch {
            setError("资产库加载失败，请重试。");
        } finally {
            setLoading(false);
        }
    }, [fetchAssets]);

    useEffect(() => {
        void refresh();
    }, [refresh]);

    const settleProcessing = useCallback(async () => {
        const pending = assets.filter((asset) => asset.status === "processing");
        if (!pending.length) return;
        const settled: AssetRef[] = [];
        for (const asset of pending) {
            try {
                const updated = await fetchAsset(asset.id);
                settled.push(updated);
            } catch {
                settled.push(asset);
            }
        }
        setAssets((current) => current.map((item) => settled.find((candidate) => candidate.id === item.id) ?? item));
    }, [assets, fetchAsset]);

    useEffect(() => {
        if (assets.some((asset) => asset.status === "processing")) {
            const timer = window.setInterval(() => {
                void settleProcessing();
            }, Math.max(200, pollIntervalMs));
            return () => window.clearInterval(timer);
        }
        return undefined;
    }, [assets, pollIntervalMs, settleProcessing]);

    const submitFile = async (file: File) => {
        if (!isPortraitImage(file) || file.size > MAX_IMAGE_BYTES) {
            setError("只支持 10MB 以内的 PNG/JPEG/WebP 人像图。");
            return;
        }
        setUploading(true);
        setError(null);
        try {
            const asset = await upload(file, () => undefined);
            setAssets((current) => [asset, ...current.filter((item) => item.id !== asset.id)]);
        } catch {
            setError("上传失败，请重试。");
        } finally {
            setUploading(false);
            if (fileInputRef.current) fileInputRef.current.value = "";
        }
    };

    const addActive = (asset: AssetRef) => {
        if (asset.status !== "active" || !targetId || typeof asset.size_bytes !== "number" || typeof asset.media_type !== "string") return;
        addToCollection(targetId, [{
            id: nanoid(),
            assetId: asset.id,
            displayName: asset.id,
            mimeType: asset.mime_type,
            bytes: asset.size_bytes,
            kind: "library",
        }]);
    };

    return (
        <aside className="fixed bottom-20 right-6 z-40 flex max-h-[60vh] w-80 flex-col rounded-xl border border-[#245a35] bg-[#07110b] p-4 shadow-2xl" aria-label="人像资产库">
            <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold">人像资产库</h2>
                <button type="button" onClick={onClose} aria-label="关闭人像资产库" className="text-xs text-[#86a991] hover:text-[#8ff0aa]">关闭</button>
            </div>
            <p className="mt-1 text-xs text-[#86a991]">上传的人像会进入火山方舟私域资产库，生成视频时以资产引用方式使用。</p>
            {targets.length ? (
                <label className="mt-3 text-xs text-[#b9d0c0]">
                    添加到素材节点
                    <select value={targetId} onChange={(event) => setTargetId(event.target.value)} aria-label="选择目标素材节点" className="mt-1 block w-full rounded border border-[#285038] bg-[#102719] px-2 py-1 text-xs text-[#8ff0aa]">
                        {targets.map((target) => (
                            <option key={target.nodeId} value={target.nodeId}>{target.label}</option>
                        ))}
                    </select>
                </label>
            ) : (
                <p className="mt-3 rounded border border-[#245a35] bg-[#0a1a10] p-2 text-xs text-[#95ad9c]">先在画布中添加一个图片素材节点，再从这里添加人像。</p>
            )}
            <div className="mt-3 flex items-center gap-2">
                <input
                    ref={fileInputRef}
                    aria-label="选择人像图片"
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    disabled={uploading}
                    onChange={(event) => {
                        const selected = event.target.files?.[0];
                        if (selected) void submitFile(selected);
                    }}
                    className="block max-w-full text-xs file:mr-3 file:rounded file:border file:border-[#285038] file:bg-[#102719] file:px-3 file:py-2 file:text-[#8ff0aa]"
                />
                <button type="button" onClick={() => void refresh()} disabled={uploading} aria-label="刷新资产库" className="shrink-0 rounded border border-[#285038] bg-[#102719] px-2 py-1 text-xs text-[#8ff0aa] disabled:opacity-50">刷新</button>
            </div>
            {error ? <p className="mt-2 text-xs text-[#e8a17d]" role="alert">{error}</p> : null}
            <div className="mt-3 flex-1 overflow-y-auto" aria-label="资产库列表">
                {loading ? <p className="text-xs text-[#86a991]">加载中…</p> : null}
                {!loading && !assets.length ? <p className="text-xs text-[#86a991]">资产库还没有人像，选择图片上传即可。</p> : null}
                <ul className="space-y-2">
                    {assets.map((asset) => (
                        <li key={asset.id} className="flex items-center justify-between rounded border border-[#1d3a26] bg-[#0a1a10] p-2">
                            <div className="min-w-0">
                                <p className="truncate text-xs text-[#b9d0c0]">{asset.id}</p>
                                <p className="text-[10px] text-[#86a991]">
                                    {asset.status === "active" ? "已就绪" : asset.status === "processing" ? "审核处理中…" : "处理失败"}
                                </p>
                            </div>
                            <button
                                type="button"
                                disabled={asset.status !== "active" || !targets.length}
                                onClick={() => addActive(asset)}
                                aria-label={`添加 ${asset.id} 到素材节点`}
                                className="shrink-0 rounded border border-[#285038] bg-[#102719] px-2 py-1 text-xs text-[#8ff0aa] disabled:cursor-not-allowed disabled:opacity-40"
                            >
                                添加
                            </button>
                        </li>
                    ))}
                </ul>
            </div>
        </aside>
    );
}
