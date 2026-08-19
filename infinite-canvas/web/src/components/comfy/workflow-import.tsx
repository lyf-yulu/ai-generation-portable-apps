import { useRef, useState } from "react";

import { importAdminComfyWorkflow, type AdminComfyWorkflow, type WorkflowImportMetadata } from "@/api/comfy-workflows";
import { ConfigExampleDownload } from "@/components/admin/config-example-download";

type Props = {
    onImport?: (file: File, metadata: WorkflowImportMetadata) => Promise<AdminComfyWorkflow>;
    onImported: (workflow: AdminComfyWorkflow) => void;
};

export function WorkflowImport({ onImport = importAdminComfyWorkflow, onImported }: Props) {
    const inputRef = useRef<HTMLInputElement>(null);
    const [file, setFile] = useState<File | null>(null);
    const [displayName, setDisplayName] = useState("");
    const [serviceId, setServiceId] = useState("");
    const [status, setStatus] = useState<"idle" | "uploading" | "succeeded" | "failed">("idle");
    const [missing, setMissing] = useState<string | null>(null);
    const locked = status === "uploading";

    const clearMissing = () => setMissing(null);

    const submit = async () => {
        if (locked) return;
        const problems: string[] = [];
        if (!file) problems.push("工作流 JSON 文件");
        if (!displayName.trim()) problems.push("工作流显示名");
        if (!serviceId.trim()) problems.push("ComfyUI 服务 ID");
        if (problems.length || !file) {
            setMissing(`请先填写：${problems.join("、")}。`);
            return;
        }
        setMissing(null);
        setStatus("uploading");
        try {
            const imported = await onImport(file, { displayName: displayName.trim(), serviceId: serviceId.trim() });
            setFile(null);
            if (inputRef.current) inputRef.current.value = "";
            setStatus("succeeded");
            onImported(imported);
        } catch {
            setStatus("failed");
        }
    };

    return (
        <section className="rounded-xl border border-[#245a35] bg-[#07110b] p-4">
            <h2 className="text-lg font-semibold">导入 ComfyUI 工作流</h2>
            <p className="mt-1 text-xs text-[#86a991]">文件仅会原样上传到同源服务端验证；页面不会读取、展示、保存或记录 JSON 内容。</p>
            <div className="mt-3">
                <ConfigExampleDownload kind="comfy-workflow" />
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-3">
                <label className="text-sm text-[#b9d0c0]">
                    选择工作流 JSON
                    <input
                        ref={inputRef}
                        aria-label="选择工作流 JSON"
                        type="file"
                        accept="application/json,.json"
                        disabled={locked}
                        onChange={(event) => {
                            setFile(event.target.files?.[0] || null);
                            setStatus("idle");
                            clearMissing();
                        }}
                        className="mt-1 block max-w-full text-xs file:mr-3 file:rounded file:border file:border-[#285038] file:bg-[#102719] file:px-3 file:py-2 file:text-[#8ff0aa]"
                    />
                </label>
                <label className="text-sm text-[#b9d0c0]">
                    工作流显示名
                    <input aria-label="工作流显示名" value={displayName} disabled={locked} onChange={(event) => { setDisplayName(event.target.value); clearMissing(); }} placeholder="例如：贝尔尼尼写真工作流" className="mt-1 block w-full rounded border border-[#3a7650] bg-[#0b1710] px-3 py-2 text-[#e5f5e9]" />
                </label>
                <label className="text-sm text-[#b9d0c0]">
                    ComfyUI 服务 ID
                    <input aria-label="ComfyUI 服务 ID" value={serviceId} disabled={locked} onChange={(event) => { setServiceId(event.target.value); clearMissing(); }} placeholder="服务声明中的 service_id，例如 comfy-local" className="mt-1 block w-full rounded border border-[#3a7650] bg-[#0b1710] px-3 py-2 text-[#e5f5e9]" />
                </label>
            </div>
            <p className="mt-2 max-w-xs truncate text-xs text-[#86a991]">{file ? `${file.name} · ${file.size} bytes` : "尚未选择文件"}</p>
            <button type="button" disabled={locked} onClick={() => void submit()} className="mt-3 rounded bg-[#42d977] px-4 py-2 text-sm font-semibold text-[#041008] disabled:opacity-40">
                {locked ? "正在导入…" : "导入工作流"}
            </button>
            {missing && (
                <p role="alert" className="mt-3 text-sm text-[#ffbd73]">
                    {missing}
                </p>
            )}
            {status === "succeeded" && (
                <p role="status" className="mt-3 text-sm text-[#58d881]">
                    工作流已导入，当前处于停用状态。
                </p>
            )}
            {status === "failed" && (
                <p role="alert" className="mt-3 text-sm text-[#ffbd73]">
                    导入失败，请检查工作流 JSON 和服务配置。
                </p>
            )}
        </section>
    );
}
