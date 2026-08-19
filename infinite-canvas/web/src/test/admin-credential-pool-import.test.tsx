import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CredentialPoolImport } from "@/components/admin/credential-pool-import";
import type { AdminCredentialPool } from "@/api/admin";


const summary: AdminCredentialPool = {
    pool_id: "banana-chiyun",
    provider_id: "chiyun-banana",
    adapter_type: "chiyun_gemini_images",
    group: "banana",
    allowed_families: ["nano-banana"],
    revision_digest: "a".repeat(64),
    key_count: 1,
    total_capacity: 2,
    capacity_status: "available",
    available_count: 1,
    busy_count: 0,
    circuit_status: "unsupported",
    circuit_open_count: null,
};

afterEach(cleanup);


describe("CredentialPoolImport", () => {
    it("does not read the file and uploads only after explicit confirmation", async () => {
        const imported = vi.fn();
        const upload = vi.fn().mockResolvedValue({ pools: [summary] });
        const file = new File(['{"api_key":"browser-must-not-read"}'], "credential-pools.json", { type: "application/json" });
        const text = vi.spyOn(file, "text");
        render(<CredentialPoolImport onImport={upload} onImported={imported} />);

        const input = screen.getByLabelText("选择凭据 JSON") as HTMLInputElement;
        fireEvent.change(input, { target: { files: [file] } });
        expect(upload).not.toHaveBeenCalled();
        expect(text).not.toHaveBeenCalled();

        fireEvent.click(screen.getByLabelText("确认替换现有凭据池"));
        fireEvent.click(screen.getByRole("button", { name: "导入并替换凭据池" }));

        await waitFor(() => expect(upload).toHaveBeenCalledWith(file));
        expect(upload).toHaveBeenCalledTimes(1);
        expect(text).not.toHaveBeenCalled();
        expect(imported).toHaveBeenCalledWith([summary]);
        expect(input.value).toBe("");
        expect(screen.getByRole("status")).toHaveTextContent("已导入 1 个凭据池");
    });

    it("locks duplicate submits and allows retry after a generic failure", async () => {
        let rejectUpload: ((reason?: unknown) => void) | undefined;
        const upload = vi.fn(() => new Promise<{ pools: AdminCredentialPool[] }>((_resolve, reject) => (rejectUpload = reject)));
        render(<CredentialPoolImport onImport={upload} onImported={vi.fn()} />);
        const file = new File(["{}"], "credential-pools.json", { type: "application/json" });
        fireEvent.change(screen.getByLabelText("选择凭据 JSON"), { target: { files: [file] } });
        fireEvent.click(screen.getByLabelText("确认替换现有凭据池"));
        const button = screen.getByRole("button", { name: "导入并替换凭据池" });
        fireEvent.click(button);
        fireEvent.click(button);
        expect(upload).toHaveBeenCalledTimes(1);

        rejectUpload?.(new Error("must not be rendered"));
        await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("导入失败，请检查 JSON 格式和服务端配置"));
        expect(screen.queryByText("must not be rendered")).not.toBeInTheDocument();
    });
});
