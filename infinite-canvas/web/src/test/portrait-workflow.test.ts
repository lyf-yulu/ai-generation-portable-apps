import { expect, it, vi } from "vitest";
import { portraitVideoWorkflow } from "@/features/workflows/portrait-video";
import { PortraitWorkflowError } from "@/features/workflows/types";

const file = new File(["image"], "portrait.png", { type: "image/png" });

it("waits through processing before submitting a portrait video", async () => {
  const calls: string[] = [];
  const output = await portraitVideoWorkflow.run({
    file, modelId: "portrait-video", prompt: "wave", params: {}, idempotencyKey: "key",
    uploadAsset: async () => { calls.push("upload:portrait"); return { id: "asset-1", kind: "portrait", status: "processing", mime_type: "image/png" }; },
    fetchAsset: async () => { calls.push("poll:asset-1"); return { id: "asset-1", kind: "portrait", status: "active", mime_type: "image/png" }; },
    submitJob: async (request) => { calls.push(`submit:${request.operation}:${request.asset_ids[0]}`); return { jobId: "job-1" }; },
    sleep: async () => {}, pollIntervalMs: 1, maxWaitMs: 2,
  });
  expect(output).toEqual({ jobId: "job-1", assetId: "asset-1" });
  expect(calls).toEqual(["upload:portrait", "poll:asset-1", "submit:video.image_to_video:asset-1"]);
});

it("reuses an existing active local asset without uploading", async () => {
  const uploadAsset = vi.fn(); const fetchAsset = vi.fn(async () => ({ id: "asset-1", kind: "portrait" as const, status: "active" as const, mime_type: "image/png" }));
  await portraitVideoWorkflow.run({ assetId: "asset-1", modelId: "portrait-video", prompt: "wave", params: {}, idempotencyKey: "key", uploadAsset, fetchAsset, submitJob: async () => ({ jobId: "job-1" }) });
  expect(uploadAsset).not.toHaveBeenCalled();
});

it("retains the local asset ID when video submission fails", async () => {
  const error = await portraitVideoWorkflow.run({ file, modelId: "portrait-video", prompt: "wave", params: {}, idempotencyKey: "key", uploadAsset: async () => ({ id: "asset-1", kind: "portrait", status: "active", mime_type: "image/png" }), fetchAsset: async () => { throw new Error("unused"); }, submitJob: async () => { throw new Error("raw upstream"); } }).catch((value) => value);
  expect(error).toBeInstanceOf(PortraitWorkflowError);
  expect(error.assetId).toBe("asset-1");
  expect(error.phase).toBe("video-submit");
  expect(error.message).not.toContain("raw upstream");
});

it("recovers a failed new upload submission by reusing its local active asset", async () => {
  const uploadAsset = vi.fn(async () => ({ id: "asset-retry", kind: "portrait" as const, status: "active" as const, mime_type: "image/png" }));
  const first = await portraitVideoWorkflow.run({
    file, modelId: "portrait-video", prompt: "wave", params: {}, idempotencyKey: "first",
    uploadAsset, fetchAsset: async () => { throw new Error("active upload is not polled"); },
    submitJob: async () => { throw new Error("submission failed"); },
  }).catch((error) => error);
  expect(first).toMatchObject({ name: "PortraitWorkflowError", phase: "video-submit", assetId: "asset-retry" });

  const fetchAsset = vi.fn(async () => ({ id: "asset-retry", kind: "portrait" as const, status: "active" as const, mime_type: "image/png" }));
  const submitJob = vi.fn(async () => ({ jobId: "job-retry" }));
  await expect(portraitVideoWorkflow.run({
    assetId: (first as PortraitWorkflowError).assetId!, modelId: "portrait-video", prompt: "wave", params: {}, idempotencyKey: "retry",
    uploadAsset, fetchAsset, submitJob,
  })).resolves.toEqual({ jobId: "job-retry", assetId: "asset-retry" });
  expect(uploadAsset).toHaveBeenCalledTimes(1);
  expect(fetchAsset).toHaveBeenCalledWith("asset-retry");
  expect(submitJob).toHaveBeenCalledWith(expect.objectContaining({ asset_ids: ["asset-retry"] }));
});

it("does not submit when a portrait asset fails or times out", async () => {
  const submitJob = vi.fn(async () => ({ jobId: "must-not-submit" }));
  const base = { file, modelId: "portrait-video", prompt: "wave", params: {}, idempotencyKey: "key", uploadAsset: async () => ({ id: "asset-1", kind: "portrait" as const, status: "processing" as const, mime_type: "image/png" }), submitJob };
  await expect(portraitVideoWorkflow.run({ ...base, fetchAsset: async () => ({ id: "asset-1", kind: "portrait", status: "failed", mime_type: "image/png" }) })).rejects.toMatchObject({ phase: "asset-poll", assetId: "asset-1" });
  await expect(portraitVideoWorkflow.run({ ...base, fetchAsset: async () => ({ id: "asset-1", kind: "portrait", status: "processing", mime_type: "image/png" }), sleep: async () => {}, pollIntervalMs: 1, maxWaitMs: 1 })).rejects.toMatchObject({ phase: "asset-poll", assetId: "asset-1" });
  expect(submitJob).not.toHaveBeenCalled();
});

it("returns a typed resolve error when another user cannot reuse the asset", async () => {
  const error = await portraitVideoWorkflow.run({
    assetId: "other-users-asset", modelId: "portrait-video", prompt: "wave", params: {}, idempotencyKey: "key",
    uploadAsset: async () => { throw new Error("must not upload"); },
    fetchAsset: async () => { throw new Error("forbidden"); },
    submitJob: async () => { throw new Error("must not submit"); },
  }).catch((value) => value);
  expect(error).toMatchObject({ name: "PortraitWorkflowError", phase: "asset-resolve", assetId: "other-users-asset" });
});
