import { useState } from "react";

import { saveAs } from "file-saver";

import { downloadAdminConfigExample } from "@/api/admin";


type Props = {
    kind: "ark-key" | "credential-pools" | "asset-library" | "comfy-workflow";
};


export function ConfigExampleDownload({ kind }: Props) {
    const [busy, setBusy] = useState(false);

    const click = async () => {
        setBusy(true);
        try {
            const { blob, filename } = await downloadAdminConfigExample(kind);
            saveAs(blob, filename);
        } catch {
            // 示例下载失败保持按钮可用;示例内容由服务端校验后下发
        } finally {
            setBusy(false);
        }
    };

    return (
        <button
            type="button"
            disabled={busy}
            onClick={() => void click()}
            className="rounded border border-[#3a7650] px-3 py-1.5 text-xs text-[#8ff0aa] disabled:opacity-40"
        >
            {busy ? "下载中…" : "下载示例 JSON"}
        </button>
    );
}
