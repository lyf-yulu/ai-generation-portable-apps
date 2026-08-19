import { ApiRequestError, apiFetch, safeApiPath } from "./client";
import type { JobRequest, JobState } from "./contracts";

export const createJob = (job: JobRequest, signal?: AbortSignal) => apiFetch<JobState>("/api/v1/jobs", { method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(job) });
export const fetchJob = (id: string, signal?: AbortSignal) => apiFetch<JobState>(`/api/v1/jobs/${encodeURIComponent(id)}`, { signal });
export const cancelJob = (id: string) => apiFetch<JobState>(`/api/v1/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
type AssetLike = { asset_id?: string; [key: string]: unknown };
export function assetIdsForReferences(references: AssetLike[]) {
    return references.map((reference) => {
        if (!reference.asset_id) throw new Error("参考资源需先上传资产后再提交任务");
        return reference.asset_id;
    });
}
type WaitOptions = { fetchJob?: typeof fetchJob; sleep?: (milliseconds: number) => Promise<void>; pollIntervalMs?: number; maxWaitMs?: number; signal?: AbortSignal };
const defaultSleep = (milliseconds: number) => new Promise<void>((resolve) => setTimeout(resolve, milliseconds));
export async function waitForJob(id: string, options: WaitOptions = {}): Promise<JobState> {
    const getJob = options.fetchJob || fetchJob;
    const sleep = options.sleep || defaultSleep;
    const pollIntervalMs = options.pollIntervalMs ?? 1_000;
    const maxWaitMs = options.maxWaitMs ?? 120_000;
    for (let elapsed = 0; elapsed <= maxWaitMs; elapsed += pollIntervalMs) {
        if (options.signal?.aborted) throw new DOMException("Aborted", "AbortError");
        const job = await getJob(id);
        if (job.status === "succeeded") return job;
        if (job.status === "failed") {
            const error = job.error;
            if (error) throw new ApiRequestError(error);
            throw new Error(`Job ${job.status}`);
        }
        if (elapsed + pollIntervalMs > maxWaitMs) break;
        await sleep(pollIntervalMs);
    }
    throw new Error(`Job ${id} timed out`);
}

export function protectedResultUrl(job: JobState) {
    if (!job.result_url) throw new Error("Job did not return a protected result URL");
    return safeApiPath(job.result_url);
}
