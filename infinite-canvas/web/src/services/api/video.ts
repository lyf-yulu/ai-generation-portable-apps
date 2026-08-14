import { nanoid } from "nanoid";
import { isMountedAssetUrl } from "@/api/client";
import { assetIdsForReferences, createJob, protectedResultUrl, waitForJob } from "@/api/jobs";
import type { AiConfig } from "@/stores/use-config-store";
import { uploadMediaFile, type UploadedFile } from "@/services/file-storage";
import type { ReferenceImage } from "@/types/image";
import type { ReferenceAudio, ReferenceVideo } from "@/types/media";
type RequestOptions = { signal?: AbortSignal };
export type VideoGenerationResult = { blob?: Blob; url?: string; mimeType?: string };
export type VideoGenerationTask = { id: string; model: string };
export type VideoGenerationTaskState = { status: "pending" } | { status: "completed"; result: VideoGenerationResult } | { status: "failed"; error: string };
export async function createVideoGenerationTask(config: AiConfig, prompt: string, references: ReferenceImage[] = [], videoReferences: ReferenceVideo[] = [], audioReferences: ReferenceAudio[] = [], options?: RequestOptions): Promise<VideoGenerationTask> {
    if (options?.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const allReferences = [...references, ...videoReferences, ...audioReferences];
    const job = await createJob({ operation: references.length ? "video.image_to_video" : "video.generate", model_id: config.model || config.videoModel, prompt, params: { size: config.size, seconds: config.videoSeconds, quality: config.vquality }, asset_ids: assetIdsForReferences(allReferences), idempotency_key: nanoid() });
    return { id: job.id, model: config.model || config.videoModel };
}
export async function pollVideoGenerationTask(_config: AiConfig, task: VideoGenerationTask, options?: RequestOptions): Promise<VideoGenerationTaskState> {
    try {
        const job = await waitForJob(task.id, { signal: options?.signal });
        return { status: "completed", result: { url: protectedResultUrl(job) } };
    } catch (error) { return { status: "failed", error: error instanceof Error ? error.message : "视频任务失败" }; }
}
export async function requestVideoGeneration(config: AiConfig, prompt: string, references: ReferenceImage[] = [], videoReferences: ReferenceVideo[] = [], audioReferences: ReferenceAudio[] = [], options?: RequestOptions): Promise<VideoGenerationResult> {
    const task = await createVideoGenerationTask(config, prompt, references, videoReferences, audioReferences, options);
    const state = await pollVideoGenerationTask(config, task, options);
    if (state.status === "completed") return state.result;
    throw new Error(state.status === "failed" ? state.error : `视频任务已提交（${task.id}）`);
}
export async function storeGeneratedVideo(result: VideoGenerationResult): Promise<UploadedFile> { if (result.blob) return uploadMediaFile(result.blob, "video"); if (result.url && isMountedAssetUrl(result.url)) return { url: result.url, storageKey: "", bytes: 0, mimeType: result.mimeType || "video/mp4" }; throw new Error("视频结果必须是受保护的资产地址"); }
