import { useCallback, useEffect, useRef, useState } from "react";
import { cancelJob, createJob, fetchJob } from "@/api/jobs";
import type { JobRequest, JobState } from "@/api/contracts";
import { captureScopedStore, isStorageLeaseActive, onStorageScopeChanged, onStorageScopeCleared, type ScopedStoreLease } from "@/storage/scope";
import { randomId } from "@/lib/utils";
import { generationErrorMessage, safeFailureMetadata } from "./error-message";
import { ApiRequestError } from "@/api/client";
import { create } from "zustand";

export type GenerationStatus = "idle" | "submitting" | "queued" | "running" | "succeeded" | "failed";
export type GenerationState = { status: GenerationStatus; jobId?: string; message?: string; retryable?: boolean };
export type PendingRef = { request: JobRequest; projectId?: string; jobId?: string; sourceNodeId?: string };
export type GenerationTask = { jobId: string; title: string; status: GenerationStatus; sourceNodeId?: string };
type GenerationTaskStore = { tasks: GenerationTask[]; upsert: (task: GenerationTask) => void; remove: (jobId: string) => void; clear: () => void };
export const useGenerationTasks = create<GenerationTaskStore>()((set) => ({
    tasks: [],
    upsert: (task) => set((state) => ({ tasks: [...state.tasks.filter((item) => item.jobId !== task.jobId), task] })),
    remove: (jobId) => set((state) => ({ tasks: state.tasks.filter((item) => item.jobId !== jobId) })),
    clear: () => set({ tasks: [] }),
}));
export function clearGenerationTasks() { useGenerationTasks.getState().clear(); }
export function dismissGenerationTask(jobId: string) { useGenerationTasks.getState().remove(jobId); }
type GenerationApi = { create: (job: JobRequest, signal?: AbortSignal) => Promise<JobState>; fetch: (id: string, signal?: AbortSignal) => Promise<JobState>; cancel?: (id: string) => Promise<JobState> };
type SubmitInput = Omit<JobRequest, "idempotency_key"> & { projectId: string; sourceNodeId?: string };
type Options = { api?: GenerationApi; pollDelayMs?: number; idempotencyKey?: () => string; onStateChanged?: (job: JobState, ref?: PendingRef) => void; onSucceeded?: (job: JobState, ref?: PendingRef) => void; onCancelled?: (details: { jobId: string; projectId?: string; sourceNodeId?: string }) => void; onFailed?: (details: { request: JobRequest; projectId?: string; sourceNodeId?: string; message: string; requestId?: string; phase?: string; retryToken?: string }) => void };
const REFS_KEY = "generation-job-refs";
const delay = (ms: number, signal: AbortSignal) => new Promise<void>((resolve, reject) => { const timer = setTimeout(resolve, ms); signal.addEventListener("abort", () => { clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); }, { once: true }); });
const stateFor = (job: JobState): GenerationStatus => job.status === "uploading" || job.status === "submitting" ? "submitting" : job.status;

export function useGenerationJob(options: Options = {}) {
    const apiRef = useRef<GenerationApi>(options.api ?? { create: createJob, fetch: fetchJob, cancel: cancelJob });
    const optionsRef = useRef(options);
    apiRef.current = options.api ?? apiRef.current;
    optionsRef.current = options;
    const [state, setState] = useState<GenerationState>({ status: "idle" });
    const controllers = useRef(new Map<string, AbortController>());
    const lease = useRef<ScopedStoreLease | null>(null);
    const refs = useRef(new Map<string, PendingRef>());
    const restoredVersion = useRef<number | null>(null);
    const restoring = useRef(new Map<number, Promise<void>>());
    const completed = useRef(new Set<string>());
    const active = useRef(true);
    const persist = useCallback(async () => {
        const current = lease.current;
        if (current && isStorageLeaseActive(current)) await current.store.setItem(REFS_KEY, [...refs.current.values()]);
    }, []);
    const publish = useCallback((next: GenerationState, captured: ScopedStoreLease | null) => {
        if (active.current && (!captured || isStorageLeaseActive(captured))) setState(next);
    }, []);
    const stop = useCallback((jobId?: string) => {
        if (jobId) { controllers.current.get(jobId)?.abort(); controllers.current.delete(jobId); return; }
        controllers.current.forEach((controller) => controller.abort()); controllers.current.clear();
    }, []);
    const restore = useCallback(async (captured: ScopedStoreLease | null) => {
        if (!captured || !isStorageLeaseActive(captured) || restoredVersion.current === captured.version) return;
        const pending = restoring.current.get(captured.version);
        if (pending) return pending;
        const task = (async () => {
            const saved = await captured.store.getItem<PendingRef[]>(REFS_KEY);
            if (!isStorageLeaseActive(captured)) return;
            refs.current.clear();
            for (const ref of saved || []) refs.current.set(ref.jobId || ref.request.idempotency_key, ref);
            restoredVersion.current = captured.version;
        })();
        restoring.current.set(captured.version, task);
        try { await task; } finally { restoring.current.delete(captured.version); }
    }, []);
    const poll = useCallback(async (jobId: string, captured: ScopedStoreLease | null) => {
        if (controllers.current.has(jobId)) return;
        const refAtStart = refs.current.get(jobId);
        useGenerationTasks.getState().upsert({ jobId, title: refAtStart?.request.prompt.slice(0, 32) || jobId, status: "queued", sourceNodeId: refAtStart?.sourceNodeId });
        const signal = new AbortController(); controllers.current.set(jobId, signal);
        let wait = optionsRef.current.pollDelayMs ?? 1_000;
        let missingAttempts = 0;
        try {
            while (!signal.signal.aborted && (!captured || isStorageLeaseActive(captured))) {
                let job: JobState;
                try {
                    job = await apiRef.current.fetch(jobId, signal.signal);
                } catch (error) {
                    if (error instanceof DOMException && error.name === "AbortError") return;
                    const missing = error instanceof ApiRequestError && error.code === "JOB_NOT_FOUND";
                    if (missing) missingAttempts += 1;
                    if (!missing || missingAttempts < 3) {
                        await delay(wait, signal.signal);
                        wait = Math.min(wait * 2, 10_000);
                        continue;
                    }
                    const ref = refs.current.get(jobId);
                    refs.current.delete(jobId);
                    await persist();
                    const message = generationErrorMessage(error);
                    publish({ status: "failed", jobId, message, retryable: true }, captured);
                    useGenerationTasks.getState().upsert({ jobId, title: ref?.request.prompt.slice(0, 32) || jobId, status: "failed", sourceNodeId: ref?.sourceNodeId });
                    if (ref) optionsRef.current.onFailed?.({ request: ref.request, projectId: ref.projectId, sourceNodeId: ref.sourceNodeId, message });
                    return;
                }
                missingAttempts = 0;
                const status = stateFor(job);
                optionsRef.current.onStateChanged?.(job, refs.current.get(jobId));
                publish({ status, jobId }, captured);
                const taskRef = refs.current.get(jobId);
                useGenerationTasks.getState().upsert({ jobId, title: taskRef?.request.prompt.slice(0, 32) || jobId, status, sourceNodeId: taskRef?.sourceNodeId });
                if (status === "succeeded") {
                    const ref = refs.current.get(jobId);
                    const operation = ref?.request.operation;
                    refs.current.delete(jobId); await persist();
                    const completeJob = { ...job, operation: job.operation ?? operation };
                    if (!completed.current.has(jobId) && (!captured || isStorageLeaseActive(captured))) { completed.current.add(jobId); optionsRef.current.onSucceeded?.(completeJob, ref); }
                    return;
                }
                if (status === "failed") {
                    const ref = refs.current.get(jobId);
                    const request = ref?.request;
                    refs.current.delete(jobId); await persist();
                    const message = generationErrorMessage(job.error ? new ApiRequestError(job.error) : new Error("failed"));
                    publish({ status, jobId, message, retryable: job.error?.retryable }, captured);
                    const safe = job.error ? { request_id: job.error.request_id, phase: job.error.phase } : undefined;
                    if (request) optionsRef.current.onFailed?.({ request, projectId: ref?.projectId, sourceNodeId: ref?.sourceNodeId, message, requestId: safe?.request_id, phase: safe?.phase });
                    return;
                }
                await delay(wait, signal.signal); wait = Math.min(wait * 2, 10_000);
            }
        } catch (error) { if (!(error instanceof DOMException && error.name === "AbortError") && (!captured || isStorageLeaseActive(captured))) { publish({ status: "failed", jobId, message: generationErrorMessage(error), retryable: true }, captured); const ref = refs.current.get(jobId); useGenerationTasks.getState().upsert({ jobId, title: ref?.request.prompt.slice(0, 32) || jobId, status: "failed", sourceNodeId: ref?.sourceNodeId }); } } finally { controllers.current.delete(jobId); }
    }, [persist, publish, stop]);
    const submit = useCallback(async (input: SubmitInput) => {
        const captured = lease.current = captureScopedStore(REFS_KEY);
        await restore(captured);
        const { projectId, sourceNodeId, ...jobInput } = input;
        const request = { ...jobInput, idempotency_key: optionsRef.current.idempotencyKey?.() ?? randomId() };
        refs.current.set(request.idempotency_key, { request, projectId, sourceNodeId }); await persist(); publish({ status: "submitting" }, captured);
        useGenerationTasks.getState().upsert({ jobId: request.idempotency_key, title: request.prompt.slice(0, 32), status: "submitting", sourceNodeId });
        try {
            const submitController = new AbortController(); controllers.current.set(request.idempotency_key, submitController);
            const job = await apiRef.current.create(request, submitController.signal);
            controllers.current.delete(request.idempotency_key);
            const ref = refs.current.get(request.idempotency_key); refs.current.delete(request.idempotency_key); refs.current.set(job.id, { request, projectId: ref?.projectId, jobId: job.id, sourceNodeId: ref?.sourceNodeId }); await persist();
            useGenerationTasks.getState().remove(request.idempotency_key);
            useGenerationTasks.getState().upsert({ jobId: job.id, title: request.prompt.slice(0, 32), status: stateFor(job), sourceNodeId });
            optionsRef.current.onStateChanged?.(job, refs.current.get(job.id));
            publish({ status: stateFor(job), jobId: job.id }, captured);
            await poll(job.id, captured);
        } catch (error) {
            controllers.current.delete(request.idempotency_key);
            const safe = safeFailureMetadata(error);
            const message = generationErrorMessage(error);
            publish({ status: "failed", message, retryable: true }, captured);
            useGenerationTasks.getState().upsert({ jobId: request.idempotency_key, title: request.prompt.slice(0, 32), status: "failed", sourceNodeId });
            optionsRef.current.onFailed?.({ request, projectId, sourceNodeId, message, requestId: safe?.request_id, phase: safe?.phase, retryToken: request.idempotency_key });
            throw error;
        }
    }, [persist, poll, publish, restore]);
    const retry = useCallback(async (retryToken: string) => {
        const captured = lease.current = captureScopedStore(REFS_KEY); await restore(captured);
        const ref = refs.current.get(retryToken);
        if (!ref || ref.jobId) throw new Error("This generation cannot be retried");
        const request = ref.request;
        try {
            const submitController = new AbortController(); controllers.current.set(request.idempotency_key, submitController);
            const job = await apiRef.current.create(request, submitController.signal);
            controllers.current.delete(request.idempotency_key); refs.current.delete(retryToken); refs.current.set(job.id, { ...ref, jobId: job.id }); await persist(); useGenerationTasks.getState().remove(retryToken); useGenerationTasks.getState().upsert({ jobId: job.id, title: request.prompt.slice(0, 32), status: stateFor(job), sourceNodeId: ref.sourceNodeId }); publish({ status: stateFor(job), jobId: job.id }, captured); await poll(job.id, captured);
        } catch (error) { controllers.current.delete(request.idempotency_key); throw error; }
    }, [persist, poll, restore]);
    const resume = useCallback(async (jobId: string) => { const captured = lease.current = captureScopedStore(REFS_KEY); await poll(jobId, captured); }, [poll]);
    const cancelQueued = useCallback(async (jobId: string) => {
        const cancel = apiRef.current.cancel;
        if (!cancel) throw new Error("This generation cannot be cancelled");
        const captured = lease.current;
        const ref = refs.current.get(jobId);
        const job = await cancel(jobId);
        optionsRef.current.onStateChanged?.(job, ref);
        const status = stateFor(job);
        if (status === "queued" || status === "running") {
            publish({ status, jobId }, captured);
            useGenerationTasks.getState().upsert({ jobId, title: ref?.request.prompt.slice(0, 32) || jobId, status, sourceNodeId: ref?.sourceNodeId });
            void poll(jobId, captured);
            return job;
        }
        if (status === "succeeded") {
            stop(jobId);
            refs.current.delete(jobId);
            await persist();
            const completeJob = { ...job, operation: job.operation ?? ref?.request.operation };
            useGenerationTasks.getState().upsert({ jobId, title: ref?.request.prompt.slice(0, 32) || jobId, status, sourceNodeId: ref?.sourceNodeId });
            publish({ status, jobId }, captured);
            if (!completed.current.has(jobId) && (!captured || isStorageLeaseActive(captured))) { completed.current.add(jobId); optionsRef.current.onSucceeded?.(completeJob, ref); }
            return completeJob;
        }
        if (status === "failed" && job.error?.code === "TASK_CANCELLED") {
            stop(jobId);
            refs.current.delete(jobId);
            await persist();
            useGenerationTasks.getState().upsert({ jobId, title: ref?.request.prompt.slice(0, 32) || jobId, status, sourceNodeId: ref?.sourceNodeId });
            publish({ status, jobId, message: "任务已取消。", retryable: false }, captured);
            optionsRef.current.onCancelled?.({ jobId, projectId: ref?.projectId, sourceNodeId: ref?.sourceNodeId });
            return job;
        }
        if (status === "failed") {
            stop(jobId);
            refs.current.delete(jobId);
            await persist();
            const message = generationErrorMessage(job.error ? new ApiRequestError(job.error) : new Error("failed"));
            publish({ status, jobId, message, retryable: job.error?.retryable }, captured);
            useGenerationTasks.getState().upsert({ jobId, title: ref?.request.prompt.slice(0, 32) || jobId, status, sourceNodeId: ref?.sourceNodeId });
            if (ref) optionsRef.current.onFailed?.({ request: ref.request, projectId: ref.projectId, sourceNodeId: ref.sourceNodeId, message, requestId: job.error?.request_id, phase: job.error?.phase });
            return job;
        }
        throw new Error("The cancellation response was invalid");
    }, [persist, poll, publish, stop]);
    useEffect(() => { active.current = true; const activate = () => { stop(); clearGenerationTasks(); refs.current.clear(); completed.current.clear(); restoredVersion.current = null; lease.current = captureScopedStore(REFS_KEY); const captured = lease.current; void (async () => { await restore(captured); if (!captured || !isStorageLeaseActive(captured)) return; for (const ref of refs.current.values()) if (ref.jobId) void poll(ref.jobId, captured); })(); }; const clear = () => { stop(); clearGenerationTasks(); }; activate(); const unsubscribe = onStorageScopeCleared(clear); const unsubscribeScope = onStorageScopeChanged(activate); return () => { active.current = false; unsubscribe(); unsubscribeScope(); stop(); }; }, [poll, restore, stop]);
    return { state, submit, retry, resume, cancelQueued, cancel: stop, failureMetadata: safeFailureMetadata };
}
