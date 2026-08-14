import { nanoid } from "nanoid";
import type { StoreApi } from "zustand";

import * as projectsApi from "@/api/projects";
import { ApiRequestError } from "@/api/client";
import { normalizeCanvasProject, type CanvasProjectInput } from "@/features/graph/normalize-project";
import { nodeRegistry } from "@/features/nodes/registry";
import { isStorageLeaseActive, onStorageScopeCleared, type ScopedStoreLease } from "@/storage/scope";
import {
    useCanvasStore,
    type CanvasProject,
    type CanvasStore,
    type ProjectSyncMetadata,
    type ProjectSyncMetadataMap,
} from "@/stores/canvas/use-canvas-store";


export type ProjectEnvelope = projectsApi.ProjectEnvelope;
export type ProjectApi = {
    list: (signal?: AbortSignal) => Promise<ProjectEnvelope[]>;
    create: (project: CanvasProject, signal?: AbortSignal) => Promise<ProjectEnvelope>;
    get: (id: string, signal?: AbortSignal) => Promise<ProjectEnvelope>;
    update: (project: CanvasProject, expectedVersion: number, signal?: AbortSignal) => Promise<ProjectEnvelope>;
    remove: (id: string, signal?: AbortSignal) => Promise<void>;
};

const defaultApi: ProjectApi = {
    list: projectsApi.listProjects,
    create: projectsApi.createProject,
    get: projectsApi.getProject,
    update: projectsApi.updateProject,
    remove: projectsApi.deleteProject,
};

function serialized(project: CanvasProjectInput) { return JSON.stringify(project); }
function canonicalJson(value: unknown): string {
    const sort = (item: unknown): unknown => {
        if (Array.isArray(item)) return item.map(sort);
        if (!item || typeof item !== "object") return item;
        const sorted: Record<string, unknown> = {};
        for (const key of Object.keys(item).sort()) {
            const child = sort((item as Record<string, unknown>)[key]);
            if (child !== undefined) sorted[key] = child;
        }
        return sorted;
    };
    return JSON.stringify(sort(value));
}
function baselineSnapshot(project: CanvasProject) {
    const { updatedAt: _clientTimestamp, ...content } = project;
    return canonicalJson(content);
}
function serverSnapshot(project: CanvasProjectInput, normalized: CanvasProject) {
    return canonicalJson(project) === canonicalJson(normalized) ? serialized(normalized) : serialized(project);
}
function hasCode(error: unknown, code: string) { return error instanceof ApiRequestError ? error.code === code : Boolean(error && typeof error === "object" && (error as { code?: unknown }).code === code); }
function isConflict(error: unknown) { return hasCode(error, "PROJECT_CONFLICT"); }
function isNotFound(error: unknown) { return hasCode(error, "PROJECT_NOT_FOUND"); }
function serverMetadata(project: CanvasProject, version: number): ProjectSyncMetadata {
    return { source: "server", version, snapshot: baselineSnapshot(project) };
}
function localCopy(project: CanvasProject, suffix: "冲突副本" | "本地恢复副本") {
    const now = new Date().toISOString();
    return { ...project, id: nanoid(), title: `${project.title}（${suffix}）`, createdAt: now, updatedAt: now };
}

export class ProjectSync {
    private generation = 0;
    private lease: ScopedStoreLease | null = null;
    private versions = new Map<string, number>();
    private snapshots = new Map<string, string>();
    private timer: ReturnType<typeof setTimeout> | null = null;
    private unsubscribe: (() => void) | null = null;
    private controllers = new Set<AbortController>();
    private flushingGenerations = new Set<number>();
    private queuedAgain = new Set<number>();
    private syncFailed = false;

    constructor(private readonly api: ProjectApi, private readonly store: Pick<StoreApi<CanvasStore>, "getState" | "subscribe">) {}

    async activate(lease: ScopedStoreLease): Promise<void> {
        this.stop();
        if (this.store.getState().loadError?.readOnly) {
            this.store.getState().setProjectsLoaded(true);
            return;
        }
        const generation = this.generation;
        this.lease = lease;
        this.store.getState().setProjectsLoaded(false);
        const localProjects = this.store.getState().projects;
        const previousMetadata = this.store.getState().projectSyncMetadata;
        const controller = this.controller();
        try {
            const serverEnvelopes = await this.api.list(controller.signal);
            if (!this.active(generation, lease)) return;
            this.versions.clear();
            this.snapshots.clear();
            const localById = new Map(localProjects.map((project) => [project.id, project]));
            const nextMetadata: ProjectSyncMetadataMap = {};
            const localFirst: CanvasProject[] = [];
            const authoritative: CanvasProject[] = [];
            let conflictCopies = 0;
            let recoveryCopies = 0;

            for (const item of serverEnvelopes) {
                const serverProject = normalizeCanvasProject(item.project, nodeRegistry);
                this.versions.set(serverProject.id, item.version);
                const serverSerialized = serverSnapshot(item.project, serverProject);
                const local = localById.get(serverProject.id);
                const metadata = previousMetadata[serverProject.id];
                localById.delete(serverProject.id);

                if (!local) {
                    if (metadata?.source === "server") {
                        // The local absence is a pending deletion. Keep its server snapshot only long enough to issue DELETE.
                        this.snapshots.set(serverProject.id, serverSerialized);
                        nextMetadata[serverProject.id] = metadata;
                    } else {
                        this.snapshots.set(serverProject.id, serverSerialized);
                        nextMetadata[serverProject.id] = serverMetadata(serverProject, item.version);
                        authoritative.push(serverProject);
                    }
                    continue;
                }

                const localChanged = metadata?.source === "server" && baselineSnapshot(local) !== metadata.snapshot;
                const serverChanged = metadata?.source === "server"
                    && (item.version !== metadata.version || baselineSnapshot(serverProject) !== metadata.snapshot);

                if (metadata?.source === "server" && localChanged && !serverChanged) {
                    this.snapshots.set(serverProject.id, serverSerialized);
                    nextMetadata[serverProject.id] = metadata;
                    authoritative.push(local);
                    continue;
                }

                if ((metadata?.source === "server" && localChanged && serverChanged)
                    || (metadata?.source !== "server" && baselineSnapshot(local) !== baselineSnapshot(serverProject))) {
                    const copy = localCopy(local, "冲突副本");
                    localFirst.push(copy);
                    nextMetadata[copy.id] = { source: "draft" };
                    conflictCopies += 1;
                }

                this.snapshots.set(serverProject.id, serverSerialized);
                nextMetadata[serverProject.id] = serverMetadata(serverProject, item.version);
                authoritative.push(serverProject);
            }

            for (const local of localById.values()) {
                const metadata = previousMetadata[local.id];
                if (metadata?.source === "draft") {
                    localFirst.push(local);
                    nextMetadata[local.id] = metadata;
                    continue;
                }
                if (metadata?.source === "server" && baselineSnapshot(local) === metadata.snapshot) continue;
                const copy = localCopy(local, "本地恢复副本");
                localFirst.push(copy);
                nextMetadata[copy.id] = { source: "draft" };
                recoveryCopies += 1;
            }

            const merged = [...localFirst, ...authoritative];
            this.store.getState().replaceProjects(merged, nextMetadata);
            this.store.getState().setProjectsLoaded(true);
            this.syncFailed = false;
            this.store.getState().setSyncNotice(
                conflictCopies > 0
                    ? "检测到其他位置的更新，已保留一个冲突副本。"
                    : recoveryCopies > 0
                        ? "原画布已删除或无法访问，本地修改已另存为恢复副本。"
                        : null,
            );
            this.unsubscribe = this.store.subscribe((state, previous) => {
                if (state.projects !== previous.projects) this.queue();
            });
            const mergedIds = new Set(merged.map((project) => project.id));
            if (merged.some((project) => this.snapshots.get(project.id) !== serialized(project))
                || [...this.snapshots.keys()].some((id) => !mergedIds.has(id))) this.queue();
        } catch (error) {
            if (this.active(generation, lease) && !(error instanceof DOMException && error.name === "AbortError")) {
                this.reportFailure();
            }
        } finally {
            this.controllers.delete(controller);
        }
    }

    async save(project: CanvasProject, expectedVersion: number, signal?: AbortSignal): Promise<ProjectEnvelope> {
        if (this.store.getState().loadError?.readOnly) throw new Error("Canvas project sync is read-only");
        return expectedVersion > 0 ? this.api.update(project, expectedVersion, signal) : this.api.create(project, signal);
    }

    stop = () => {
        const generation = this.generation;
        const lease = this.lease;
        if (lease && this.active(generation, lease) && !this.store.getState().loadError?.readOnly) this.store.getState().setProjectsLoaded(false);
        this.generation += 1;
        if (this.timer) clearTimeout(this.timer);
        this.timer = null;
        this.unsubscribe?.();
        this.unsubscribe = null;
        for (const controller of this.controllers) controller.abort();
        this.controllers.clear();
        this.lease = null;
        this.versions.clear();
        this.snapshots.clear();
        this.syncFailed = false;
    };

    private active(generation: number, lease: ScopedStoreLease) { return this.generation === generation && this.lease === lease && isStorageLeaseActive(lease); }
    private controller() { const controller = new AbortController(); this.controllers.add(controller); return controller; }
    private queue() {
        if (!this.lease || !isStorageLeaseActive(this.lease) || this.store.getState().loadError?.readOnly) return;
        if (this.timer) clearTimeout(this.timer);
        this.timer = setTimeout(() => { this.timer = null; void this.flush(); }, 400);
    }

    private async flush() {
        const lease = this.lease;
        const generation = this.generation;
        if (!lease || !this.active(generation, lease) || this.store.getState().loadError?.readOnly) return;
        if (this.flushingGenerations.has(generation)) {
            this.queuedAgain.add(generation);
            return;
        }
        this.flushingGenerations.add(generation);
        try {
            await this.flushOnce(lease, generation);
        } finally {
            this.flushingGenerations.delete(generation);
            if (this.queuedAgain.delete(generation) && this.active(generation, lease)) this.queue();
        }
    }

    private async flushOnce(lease: ScopedStoreLease, generation: number) {
        if (this.store.getState().loadError?.readOnly) return;
        const projects = this.store.getState().projects;
        const currentIds = new Set(projects.map((project) => project.id));
        for (const id of [...this.snapshots.keys()]) {
            if (currentIds.has(id)) continue;
            const controller = this.controller();
            try {
                await this.api.remove(id, controller.signal);
                if (!this.active(generation, lease)) return;
                this.snapshots.delete(id);
                this.versions.delete(id);
                this.store.getState().setProjectSyncMetadata(id, null);
                this.reportSuccess();
            } catch (error) {
                if (!this.active(generation, lease)) return;
                if (isNotFound(error)) {
                    this.snapshots.delete(id);
                    this.versions.delete(id);
                    this.store.getState().setProjectSyncMetadata(id, null);
                    this.reportSuccess();
                } else if (!(error instanceof DOMException && error.name === "AbortError")) this.reportFailure();
            } finally { this.controllers.delete(controller); }
        }
        for (const project of projects) {
            const localSnapshot = serialized(project);
            if (this.snapshots.get(project.id) === localSnapshot) continue;
            const controller = this.controller();
            try {
                const result = await this.save(project, this.versions.get(project.id) || 0, controller.signal);
                if (!this.active(generation, lease)) return;
                this.versions.set(project.id, result.version);
                this.snapshots.set(project.id, localSnapshot);
                this.store.getState().setProjectSyncMetadata(project.id, serverMetadata(project, result.version));
                this.reportSuccess();
                if (serialized(this.store.getState().projects.find((item) => item.id === project.id) || project) !== localSnapshot) this.queue();
            } catch (error) {
                if (!this.active(generation, lease)) return;
                if (isConflict(error)) await this.preserveConflict(project, generation, lease);
                else if (isNotFound(error)) this.preserveMissing(project);
                else if (!(error instanceof DOMException && error.name === "AbortError")) this.reportFailure();
            } finally { this.controllers.delete(controller); }
        }
    }

    private async preserveConflict(project: CanvasProject, generation: number, lease: ScopedStoreLease) {
        const controller = this.controller();
        try {
            const server = await this.api.get(project.id, controller.signal);
            if (!this.active(generation, lease)) return;
            const serverProject = normalizeCanvasProject(server.project, nodeRegistry);
            const latestLocal = this.store.getState().projects.find((item) => item.id === project.id) || project;
            const copy = localCopy(latestLocal, "冲突副本");
            const state = this.store.getState();
            const remaining = state.projects.filter((item) => item.id !== project.id);
            const metadata = { ...state.projectSyncMetadata, [copy.id]: { source: "draft" } as const, [serverProject.id]: serverMetadata(serverProject, server.version) };
            this.versions.set(serverProject.id, server.version);
            this.snapshots.set(serverProject.id, serverSnapshot(server.project, serverProject));
            this.syncFailed = false;
            this.store.getState().replaceProjects([copy, serverProject, ...remaining], metadata);
            this.store.getState().setSyncNotice("检测到其他位置的更新，已保留一个冲突副本。");
        } catch (error) {
            if (!this.active(generation, lease)) return;
            if (isNotFound(error)) this.preserveMissing(project);
            else if (!(error instanceof DOMException && error.name === "AbortError")) {
                this.syncFailed = true;
                this.store.getState().setSyncNotice("项目发生版本冲突，本地修改仍保留在本机。");
            }
        } finally { this.controllers.delete(controller); }
    }

    private preserveMissing(project: CanvasProject) {
        const state = this.store.getState();
        const latestLocal = state.projects.find((item) => item.id === project.id) || project;
        const copy = localCopy(latestLocal, "本地恢复副本");
        const remaining = state.projects.filter((item) => item.id !== project.id);
        const metadata = { ...state.projectSyncMetadata, [copy.id]: { source: "draft" } as const };
        delete metadata[project.id];
        this.versions.delete(project.id);
        this.snapshots.delete(project.id);
        this.syncFailed = false;
        this.store.getState().replaceProjects([copy, ...remaining], metadata);
        this.store.getState().setProjectsLoaded(true);
        this.store.getState().setSyncNotice("原画布已删除或无法访问，本地修改已另存为恢复副本。");
    }

    private reportFailure() {
        this.syncFailed = true;
        this.store.getState().setSyncNotice("项目暂时无法同步，当前修改仍保留在本机。");
    }

    private reportSuccess() {
        if (this.syncFailed) {
            this.syncFailed = false;
            this.store.getState().setSyncNotice("项目已恢复同步。");
        } else if (this.store.getState().syncNotice === "项目已恢复同步。") {
            this.store.getState().setSyncNotice(null);
        }
    }
}

export const projectSync = new ProjectSync(defaultApi, useCanvasStore);
onStorageScopeCleared(projectSync.stop);
