import type { AssetRef } from "@/api/contracts";
import { PortraitWorkflowError, type PortraitVideoInput, type PortraitVideoOutput, type WorkflowDefinition } from "./types";

const defaultSleep = (milliseconds: number) => new Promise<void>((resolve) => setTimeout(resolve, milliseconds));

async function waitForActiveAsset(asset: AssetRef, input: PortraitVideoInput) {
    const interval = input.pollIntervalMs ?? 1_000;
    const maximum = input.maxWaitMs ?? 120_000;
    const sleep = input.sleep ?? defaultSleep;
    for (let elapsed = 0; elapsed <= maximum; elapsed += interval) {
        const current = elapsed === 0 ? asset : await input.fetchAsset(asset.id);
        if (current.status === "active") return current;
        if (current.status === "failed") throw new Error(`asset ${current.id} failed`);
        if (elapsed + interval > maximum) break;
        await sleep(interval);
    }
    throw new Error(`asset ${asset.id} timed out`);
}

function validatePollingLimits(input: PortraitVideoInput) {
    if (input.pollIntervalMs !== undefined && (!Number.isFinite(input.pollIntervalMs) || input.pollIntervalMs <= 0)) throw new Error("pollIntervalMs must be a finite positive number");
    if (input.maxWaitMs !== undefined && (!Number.isFinite(input.maxWaitMs) || input.maxWaitMs <= 0)) throw new Error("maxWaitMs must be a finite positive number");
}

export const portraitVideoWorkflow: WorkflowDefinition<PortraitVideoInput, PortraitVideoOutput> = {
    id: "portrait.video",
    version: 1,
    async run(input) {
        validatePollingLimits(input);
        if (!input.file && !input.assetId) throw new Error("file or assetId is required");
        let initial;
        try {
            initial = input.assetId
                ? await input.fetchAsset(input.assetId)
                : await input.uploadAsset(input.file!, "portrait");
        } catch (error) {
            throw new PortraitWorkflowError(input.assetId ? "asset-resolve" : "asset-upload", error, input.assetId);
        }
        let asset;
        try { asset = await waitForActiveAsset(initial, input); }
        catch (error) { throw new PortraitWorkflowError("asset-poll", error, initial.id); }
        try {
            const job = await input.submitJob({ operation: "video.image_to_video", model_id: input.modelId, prompt: input.prompt, params: input.params, asset_ids: [asset.id], idempotency_key: input.idempotencyKey });
            return { jobId: job.jobId, assetId: asset.id };
        } catch (error) {
            throw new PortraitWorkflowError("video-submit", error, asset.id);
        }
    },
};
