import { expect, it, vi } from "vitest";
import { ApiRequestError } from "@/api/client";
import { waitForJob } from "@/api/jobs";

it("polls queued jobs until a protected asset result succeeds", async () => {
    const fetchJob = vi.fn().mockResolvedValueOnce({ id: "j", status: "queued" }).mockResolvedValueOnce({ id: "j", status: "running" }).mockResolvedValueOnce({ id: "j", status: "succeeded", result: { asset_id: "asset-1", mime_type: "image/png" } });
    const sleep = vi.fn().mockResolvedValue(undefined);
    await expect(waitForJob("j", { fetchJob, sleep, pollIntervalMs: 1, maxWaitMs: 10 })).resolves.toMatchObject({ status: "succeeded" });
    expect(sleep).toHaveBeenCalledTimes(2);
});

it("preserves structured API errors from a failed job", async () => {
    const error = { code: "rate_limited", message: "Retry later", retryable: true, request_id: "req-1", phase: "submit" };
    await expect(waitForJob("j", { fetchJob: vi.fn().mockResolvedValue({ id: "j", status: "failed", error }), sleep: vi.fn() })).rejects.toBeInstanceOf(ApiRequestError);
    await expect(waitForJob("j", { fetchJob: vi.fn().mockResolvedValue({ id: "j", status: "failed", error }), sleep: vi.fn() })).rejects.toMatchObject(error);
});

it("fails only on terminal failure or timeout", async () => {
    const pending = vi.fn().mockResolvedValue({ id: "j", status: "queued" });
    await expect(waitForJob("j", { fetchJob: pending, sleep: vi.fn().mockResolvedValue(undefined), pollIntervalMs: 1, maxWaitMs: 2 })).rejects.toThrow("timed out");
    await expect(waitForJob("j", { fetchJob: vi.fn().mockResolvedValue({ id: "j", status: "failed", error: { code: "failed", message: "failed", retryable: false, request_id: "req", phase: "run" } }), sleep: vi.fn() })).rejects.toThrow("failed");
});
