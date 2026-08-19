import { useRef, useState } from "react";

import { importAdminArkKey, type AdminArkKey } from "@/api/admin";
import { ConfigExampleDownload } from "@/components/admin/config-example-download";


type Props = {
    onImport?: (file: File) => Promise<AdminArkKey>;
    onImported?: (summary: AdminArkKey) => void;
};


export function ArkKeyImport({ onImport = importAdminArkKey, onImported }: Props) {
    const inputRef = useRef<HTMLInputElement>(null);
    const [file, setFile] = useState<File | null>(null);
    const [confirmed, setConfirmed] = useState(false);
    const [status, setStatus] = useState<"idle" | "uploading" | "succeeded" | "failed">("idle");
    const locked = status === "uploading";

    const clearSelection = () => {
        setFile(null);
        setConfirmed(false);
        if (inputRef.current) inputRef.current.value = "";
    };

    const submit = async () => {
        if (!file || !confirmed || locked) return;
        setStatus("uploading");
        try {
            const result = await onImport(file);
            onImported?.(result);
            setStatus("succeeded");
        } catch {
            setStatus("failed");
        } finally {
            clearSelection();
        }
    };

    return (
        <section className="mt-6 rounded-xl border border-[#245a35] bg-[#07110b] p-4">
            <h2 className="text-lg font-semibold">导入方舟生成 Key</h2>
            <p className="mt-1 text-xs text-[#86a991]">文件只会原样上传到服务端验证；页面不会读取、展示或保存 Key，导入后新任务立即生效，无需重启。</p>
            <div className="mt-4 flex flex-wrap items-end gap-3">
                <ConfigExampleDownload kind="ark-key" />
                <label className="text-sm text-[#b9d0c0]">
                    选择 Key JSON
                    <input
                        ref={inputRef}
                        aria-label="选择 Key JSON"
                        type="file"
                        accept="application/json,.json"
                        disabled={locked}
                        onChange={(event) => {
                            const selected = event.target.files?.[0] || null;
                            setFile(selected);
                            setConfirmed(false);
                            setStatus("idle");
                        }}
                        className="mt-1 block max-w-full text-xs file:mr-3 file:rounded file:border file:border-[#285038] file:bg-[#102719] file:px-3 file:py-2 file:text-[#8ff0aa]"
                    />
                </label>
                <span className="max-w-xs truncate text-xs text-[#86a991]">{file ? `${file.name} · ${file.size} bytes` : "尚未选择文件"}</span>
            </div>
            <label className="mt-3 flex items-start gap-2 text-xs text-[#c5d7ca]">
                <input
                    type="checkbox"
                    aria-label="确认替换现有方舟 Key"
                    checked={confirmed}
                    disabled={!file || locked}
                    onChange={(event) => setConfirmed(event.target.checked)}
                    className="mt-0.5 accent-[#58ed87]"
                />
                确认替换现有方舟 Key；新任务使用新 Key，已提交任务不会重放。
            </label>
            <button
                type="button"
                disabled={!file || !confirmed || locked}
                onClick={() => void submit()}
                className="mt-3 rounded bg-[#42d977] px-4 py-2 text-sm font-semibold text-[#041008] disabled:opacity-40"
            >
                {locked ? "正在导入…" : "导入并替换方舟 Key"}
            </button>
            {status === "succeeded" && (
                <p role="status" className="mt-3 text-sm text-[#58ed87]">
                    方舟 Key 已导入，新任务立即生效。
                </p>
            )}
            {status === "failed" && (
                <p role="alert" className="mt-3 text-sm text-[#ffbd73]">
                    导入失败：请检查文件格式（{`{"version": 1, "api_key": "…"}`}）后重试。
                </p>
            )}
        </section>
    );
}
