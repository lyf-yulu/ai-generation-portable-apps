import { useCallback, useEffect, useRef, useState } from "react";

import { ConfigExampleDownload } from "@/components/admin/config-example-download";
import {
    fetchAdminAssetLibrary,
    fetchAdminAssetLibraryGroups,
    importAdminAssetLibrary,
    type AdminAssetLibrary,
    type AdminAssetLibraryGroup,
} from "@/api/admin";


type Dependencies = {
    fetchSummary?: () => Promise<AdminAssetLibrary>;
    fetchGroups?: () => Promise<AdminAssetLibraryGroup[]>;
    onImport?: (file: File) => Promise<AdminAssetLibrary>;
};

export function AdminAssetLibraryContent({ fetchSummary = fetchAdminAssetLibrary, fetchGroups = fetchAdminAssetLibraryGroups, onImport = importAdminAssetLibrary }: Dependencies) {
    const [summary, setSummary] = useState<AdminAssetLibrary | null>(null);
    const [groups, setGroups] = useState<AdminAssetLibraryGroup[]>([]);
    const [error, setError] = useState<string | null>(null);
    const [importing, setImporting] = useState(false);
    const [confirmed, setConfirmed] = useState(false);
    const [selected, setSelected] = useState<File | null>(null);
    const inputRef = useRef<HTMLInputElement>(null);

    const refresh = useCallback(async () => {
        try {
            setError(null);
            setSummary(await fetchSummary());
            setGroups(await fetchGroups());
        } catch {
            setError("资产库信息加载失败，请重试。");
        }
    }, [fetchSummary, fetchGroups]);

    useEffect(() => {
        void refresh();
    }, [refresh]);

    const submit = async () => {
        if (!selected || !confirmed || importing) return;
        setImporting(true);
        try {
            setSummary(await onImport(selected));
            setGroups(await fetchGroups());
        } catch {
            setError("导入失败：请检查 JSON 格式与文件权限。");
        } finally {
            setImporting(false);
            setSelected(null);
            setConfirmed(false);
            if (inputRef.current) inputRef.current.value = "";
        }
    };

    return (
        <div className="mx-auto max-w-3xl px-4 py-8">
            <h1 className="text-2xl font-semibold">人像资产库配置</h1>
            <p className="mt-2 text-sm text-[#95ad9c]">方舟 OpenAPI 与 TOS 凭据只保存在服务端；页面只显示是否已配置，绝不读取或回显密钥。</p>
            {error ? <p className="mt-4 rounded border border-[#7a4a32] bg-[#1d1208] p-2 text-xs text-[#e8a17d]" role="alert">{error}</p> : null}
            <section className="mt-6 rounded-xl border border-[#245a35] bg-[#07110b] p-4" aria-label="资产库状态">
                <h2 className="text-lg font-semibold">服务状态</h2>
                {summary ? (
                    <dl className="mt-3 grid grid-cols-2 gap-2 text-sm">
                        <dt className="text-[#86a991]">服务启用</dt>
                        <dd className="text-[#b9d0c0]">{summary.enabled ? "是" : "否"}</dd>
                        <dt className="text-[#86a991]">方舟 AK/SK</dt>
                        <dd className="text-[#b9d0c0]">{summary.has_ark_access ? "已配置" : "未配置"}</dd>
                        <dt className="text-[#86a991]">TOS 对象存储</dt>
                        <dd className="text-[#b9d0c0]">{summary.has_tos_access ? "已配置" : "未配置"}</dd>
                        <dt className="text-[#86a991]">存储桶</dt>
                        <dd className="text-[#b9d0c0]">{summary.tos_bucket ?? "—"}</dd>
                        <dt className="text-[#86a991]">区域</dt>
                        <dd className="text-[#b9d0c0]">{summary.tos_region ?? "—"}</dd>
                        <dt className="text-[#86a991]">项目名</dt>
                        <dd className="text-[#b9d0c0]">{summary.project_name ?? "—"}</dd>
                        <dt className="text-[#86a991]">默认分组</dt>
                        <dd className="text-[#b9d0c0]">{summary.default_group_id ?? "尚未创建"}</dd>
                    </dl>
                ) : (
                    <p className="mt-2 text-sm text-[#86a991]">加载中…</p>
                )}
            </section>
            <section className="mt-6 rounded-xl border border-[#245a35] bg-[#07110b] p-4" aria-label="导入配置">
                <h2 className="text-lg font-semibold">导入资产库 JSON</h2>
                <p className="mt-1 text-xs text-[#86a991]">文件只会原样上传到服务端验证；页面不会读取、展示或保存任何凭据。</p>
                <div className="mt-4 flex flex-wrap items-end gap-3">
                    <ConfigExampleDownload kind="asset-library" />
                    <label className="text-sm text-[#b9d0c0]">
                        选择配置 JSON
                        <input
                            ref={inputRef}
                            aria-label="选择资产库配置 JSON"
                            type="file"
                            accept="application/json,.json"
                            disabled={importing}
                            onChange={(event) => {
                                const file = event.target.files?.[0] || null;
                                setSelected(file);
                                setConfirmed(false);
                            }}
                            className="mt-1 block max-w-full text-xs file:mr-3 file:rounded file:border file:border-[#285038] file:bg-[#102719] file:px-3 file:py-2 file:text-[#8ff0aa]"
                        />
                    </label>
                    <label className="flex items-center gap-2 text-sm text-[#b9d0c0]">
                        <input type="checkbox" checked={confirmed} disabled={!selected || importing} onChange={(event) => setConfirmed(event.target.checked)} />
                        确认覆盖服务端配置
                    </label>
                    <button
                        type="button"
                        disabled={!selected || !confirmed || importing}
                        onClick={() => void submit()}
                        className="rounded border border-[#285038] bg-[#102719] px-3 py-1 text-sm text-[#8ff0aa] disabled:cursor-not-allowed disabled:opacity-40"
                    >
                        {importing ? "导入中…" : "导入"}
                    </button>
                </div>
            </section>
            <section className="mt-6 rounded-xl border border-[#245a35] bg-[#07110b] p-4" aria-label="AIGC 分组">
                <h2 className="text-lg font-semibold">AIGC 分组</h2>
                {groups.length ? (
                    <ul className="mt-3 space-y-1">
                        {groups.map((group) => (
                            <li key={group.group_id} className="text-sm text-[#b9d0c0]">{group.name} · {group.group_id}</li>
                        ))}
                    </ul>
                ) : (
                    <p className="mt-2 text-sm text-[#86a991]">尚未创建分组；首次上传人像时会自动创建默认分组。</p>
                )}
            </section>
        </div>
    );
}

export default function AdminAssetLibraryPage() {
    return <AdminAssetLibraryContent />;
}
