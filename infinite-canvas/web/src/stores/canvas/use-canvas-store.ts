import { create } from "zustand";
import { persist, type PersistStorage, type StorageValue } from "zustand/middleware";

import { nanoid } from "nanoid";
import { normalizeViewport } from "@/features/canvas/viewport";
import { GRAPH_SCHEMA_VERSION } from "@/features/graph/contracts";
import { normalizeCanvasProject, normalizeCanvasProjectBaselineSnapshot, UnsupportedGraphSchemaError, type CanvasProjectInput } from "@/features/graph/normalize-project";
import { nodeRegistry } from "@/features/nodes/registry";
import { captureAppStorageLease, localForageStorage, setItemForLease } from "@/lib/localforage-storage";
import { isStorageLeaseActive, onStorageScopeCleared, type ScopedStoreLease } from "@/storage/scope";
import type { CanvasBackgroundMode } from "@/lib/canvas-theme";
import type { CanvasAssistantSession, CanvasConnection, CanvasNodeData, ViewportTransform } from "@/types/canvas";

export type CanvasProject = {
    id: string;
    title: string;
    createdAt: string;
    updatedAt: string;
    nodes: CanvasNodeData[];
    connections: CanvasConnection[];
    chatSessions: CanvasAssistantSession[];
    activeChatId: string | null;
    backgroundMode: CanvasBackgroundMode;
    showImageInfo: boolean;
    viewport: ViewportTransform;
    graphSchemaVersion: number;
};

export type ProjectSyncMetadata =
    | { source: "draft" }
    | { source: "legacy" }
    | { source: "server"; version: number; snapshot: string };

export type ProjectSyncMetadataMap = Record<string, ProjectSyncMetadata>;

export type CanvasLoadError = {
    code: "UNSUPPORTED_GRAPH_SCHEMA" | "CANVAS_LOAD_FAILED";
    message: string;
    readOnly: true;
};

export class CanvasReadOnlyError extends Error {
    readonly code = "CANVAS_READ_ONLY";

    constructor() {
        super("Canvas data is protected in read-only mode");
        this.name = "CanvasReadOnlyError";
    }
}

export type CanvasStore = {
    hydrated: boolean;
    projectsLoaded: boolean;
    projects: CanvasProject[];
    projectSyncMetadata: ProjectSyncMetadataMap;
    syncNotice: string | null;
    loadError: CanvasLoadError | null;
    createProject: (title?: string) => string;
    importProject: (project: Partial<CanvasProjectInput>) => string;
    openProject: (id: string) => CanvasProject | null;
    renameProject: (id: string, title: string) => void;
    deleteProjects: (ids: string[]) => void;
    replaceProjects: (projects: CanvasProjectInput[], projectSyncMetadata?: ProjectSyncMetadataMap) => void;
    setProjectSyncMetadata: (id: string, metadata: ProjectSyncMetadata | null) => void;
    setProjectsLoaded: (loaded: boolean) => void;
    setSyncNotice: (notice: string | null) => void;
    updateProject: (id: string, patch: Partial<Pick<CanvasProject, "nodes" | "connections" | "chatSessions" | "activeChatId" | "backgroundMode" | "showImageInfo" | "viewport">>) => void;
};

const initialViewport: ViewportTransform = { x: 0, y: 0, k: 1 };
const CANVAS_STORE_KEY = "infinite-canvas:canvas_store";
type PersistedCanvasState = Pick<CanvasStore, "projects" | "projectSyncMetadata">;
let saveTimer: ReturnType<typeof setTimeout> | null = null;
let queuedPersistState: PersistedCanvasState | null = null;
let queuedLease: ScopedStoreLease | null = null;
let blockedPersistScopeVersion: number | null = null;

function cancelQueuedCanvasPersist() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = null;
    queuedPersistState = null;
    queuedLease = null;
}

function isPersistBlocked(lease: ScopedStoreLease) {
    return blockedPersistScopeVersion === lease.version;
}

export function clearCanvasInMemory() {
    cancelQueuedCanvasPersist();
    useCanvasStore.setState({ projects: [], projectSyncMetadata: {}, hydrated: false, projectsLoaded: false, syncNotice: null, loadError: null });
}

export function migrateCanvasPersistedState(persistedState: unknown, persistedVersion: number) {
    const state = persistedState && typeof persistedState === "object" ? persistedState as { projects?: CanvasProjectInput[]; projectSyncMetadata?: ProjectSyncMetadataMap } : {};
    const sourceProjects = Array.isArray(state.projects) ? state.projects : [];
    const projects = sourceProjects.map((project) => normalizeCanvasProject(project, nodeRegistry));
    if (persistedVersion >= 1 && state.projectSyncMetadata && typeof state.projectSyncMetadata === "object") {
        const projectSyncMetadata = { ...state.projectSyncMetadata };
        for (const source of sourceProjects) {
            const metadata = projectSyncMetadata[source.id];
            if (metadata?.source !== "server") continue;
            try {
                projectSyncMetadata[source.id] = { ...metadata, snapshot: normalizeCanvasProjectBaselineSnapshot(metadata.snapshot, source, nodeRegistry) };
            } catch {
                // Preserve an unreadable baseline so sync recovery remains conservative.
            }
        }
        return { ...state, projects, projectSyncMetadata };
    }
    const projectSyncMetadata: ProjectSyncMetadataMap = {};
    for (const project of projects) projectSyncMetadata[project.id] = { source: "legacy" };
    return { ...state, projects, projectSyncMetadata };
}

const canvasStorage: PersistStorage<PersistedCanvasState> = {
    getItem: async (name) => {
        const value = await localForageStorage.getItem(name);
        if (!value) return null;
        const parsed = JSON.parse(value) as StorageValue<PersistedCanvasState>;
        queuedPersistState = parsed.state;
        return parsed;
    },
    setItem: (name, value) => {
        const nextState = value.state as PersistedCanvasState;
        if (queuedPersistState && queuedPersistState.projects === nextState.projects && queuedPersistState.projectSyncMetadata === nextState.projectSyncMetadata) return;
        const lease = captureAppStorageLease();
        if (!lease || isPersistBlocked(lease)) return;
        queuedPersistState = nextState;
        queuedLease = lease;
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
            saveTimer = null;
            const scheduledLease = queuedLease;
            queuedLease = null;
            if (scheduledLease && !isPersistBlocked(scheduledLease)) void setItemForLease(scheduledLease, name, JSON.stringify(value));
        }, 400);
    },
    removeItem: (name) => localForageStorage.removeItem(name),
};

export const useCanvasStore = create<CanvasStore>()(
    persist(
        (set, get) => ({
            hydrated: false,
            projectsLoaded: false,
            projects: [],
            projectSyncMetadata: {},
            syncNotice: null,
            loadError: null,
            createProject: (title = "未命名画布") => {
                if (get().loadError?.readOnly) throw new CanvasReadOnlyError();
                const now = new Date().toISOString();
                const id = nanoid();
                const project: CanvasProject = {
                    id,
                    title,
                    createdAt: now,
                    updatedAt: now,
                    nodes: [],
                    connections: [],
                    chatSessions: [],
                    activeChatId: null,
                    backgroundMode: "lines",
                    showImageInfo: false,
                    viewport: initialViewport,
                    graphSchemaVersion: GRAPH_SCHEMA_VERSION,
                };
                set((state) => ({
                    projects: [project, ...state.projects],
                    projectSyncMetadata: { ...state.projectSyncMetadata, [id]: { source: "draft" } },
                }));
                return id;
            },
            importProject: (source) => {
                if (get().loadError?.readOnly) throw new CanvasReadOnlyError();
                const now = new Date().toISOString();
                const project = normalizeCanvasProject({
                    id: nanoid(),
                    title: source.title || "导入画布",
                    createdAt: source.createdAt || now,
                    updatedAt: now,
                    nodes: source.nodes || [],
                    connections: source.connections || [],
                    chatSessions: source.chatSessions || [],
                    activeChatId: source.activeChatId || null,
                    backgroundMode: source.backgroundMode || "lines",
                    showImageInfo: source.showImageInfo || false,
                    viewport: normalizeViewport(source.viewport),
                    ...(Object.prototype.hasOwnProperty.call(source, "graphSchemaVersion") ? { graphSchemaVersion: source.graphSchemaVersion } : {}),
                }, nodeRegistry);
                set((state) => ({
                    projects: [project, ...state.projects],
                    projectSyncMetadata: { ...state.projectSyncMetadata, [project.id]: { source: "draft" } },
                }));
                return project.id;
            },
            openProject: (id) => {
                return get().projects.find((item) => item.id === id) || null;
            },
            renameProject: (id, title) => {
                if (get().loadError?.readOnly) return;
                set((state) => ({
                    projects: state.projects.map((project) => (project.id === id ? { ...project, title: title.trim() || project.title, updatedAt: new Date().toISOString() } : project)),
                }));
            },
            deleteProjects: (ids) => {
                if (get().loadError?.readOnly) return;
                set((state) => {
                    const projects = state.projects.filter((project) => !ids.includes(project.id));
                    const projectSyncMetadata = { ...state.projectSyncMetadata };
                    for (const id of ids) {
                        if (projectSyncMetadata[id]?.source !== "server") delete projectSyncMetadata[id];
                    }
                    return { projects, projectSyncMetadata };
                });
            },
            replaceProjects: (projects, projectSyncMetadata) => {
                if (get().loadError?.readOnly) return;
                const normalized = projects.map((project) => normalizeCanvasProject(project, nodeRegistry));
                set(projectSyncMetadata ? { projects: normalized, projectSyncMetadata } : { projects: normalized });
            },
            setProjectSyncMetadata: (id, metadata) => {
                if (get().loadError?.readOnly) return;
                set((state) => {
                const projectSyncMetadata = { ...state.projectSyncMetadata };
                if (metadata) projectSyncMetadata[id] = metadata;
                else delete projectSyncMetadata[id];
                return { projectSyncMetadata };
                });
            },
            setProjectsLoaded: (projectsLoaded) => set({ projectsLoaded }),
            setSyncNotice: (syncNotice) => set({ syncNotice }),
            updateProject: (id, patch) => {
                if (get().loadError?.readOnly) return;
                set((state) => ({
                    projects: state.projects.map((project) => (project.id === id ? { ...project, ...patch, updatedAt: new Date().toISOString() } : project)),
                }));
            },
        }),
        {
            name: CANVAS_STORE_KEY,
            storage: canvasStorage,
            version: 2,
            migrate: migrateCanvasPersistedState,
            merge: (persistedState, currentState) => ({
                ...currentState,
                ...migrateCanvasPersistedState(persistedState, 2),
            }),
            partialize: (state) =>
                ({
                    projects: state.projects,
                    projectSyncMetadata: state.projectSyncMetadata,
                }),
            onRehydrateStorage: () => {
                const lease = captureAppStorageLease();
                return (_state, error) => {
                    if (error && lease && isStorageLeaseActive(lease)) {
                        blockedPersistScopeVersion = lease.version;
                        cancelQueuedCanvasPersist();
                        const unsupported = error instanceof UnsupportedGraphSchemaError;
                        useCanvasStore.setState({
                            hydrated: true,
                            projectsLoaded: false,
                            loadError: {
                                code: unsupported ? "UNSUPPORTED_GRAPH_SCHEMA" : "CANVAS_LOAD_FAILED",
                                message: unsupported ? "此画布由更新版本创建，请升级应用；升级前将以只读方式保护原始数据。" : "画布数据无法安全加载，原始数据已进入只读保护。",
                                readOnly: true,
                            },
                        });
                        return;
                    }
                    useCanvasStore.setState({ hydrated: true, loadError: null });
                };
            },
        },
    ),
);

onStorageScopeCleared(() => {
    blockedPersistScopeVersion = null;
    cancelQueuedCanvasPersist();
});
