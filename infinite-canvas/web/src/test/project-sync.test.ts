import { afterEach, expect, it, vi } from "vitest";

import { ProjectSync, type ProjectApi, type ProjectEnvelope } from "@/features/projects/project-sync";
import { GRAPH_SCHEMA_VERSION } from "@/features/graph/contracts";
import type { CanvasProjectInput } from "@/features/graph/normalize-project";
import { captureAppStorageLease } from "@/lib/localforage-storage";
import { clearCanvasInMemory, migrateCanvasPersistedState, type CanvasProject, useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { clearStorageScope, setScopedStoreFactoryForTest, setStorageScope } from "@/storage/scope";


function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((done) => { resolve = done; });
    return { promise, resolve };
}

function projectFor(id: string, title = id, updatedAt = "2026-08-10T00:00:00.000Z"): CanvasProject {
    return { id, title, createdAt: updatedAt, updatedAt, nodes: [], connections: [], chatSessions: [], activeChatId: null, backgroundMode: "lines", showImageInfo: false, viewport: { x: 0, y: 0, k: 1 }, graphSchemaVersion: GRAPH_SCHEMA_VERSION };
}

function envelope(project: CanvasProjectInput, version = 1): ProjectEnvelope { return { project, version }; }
function canonicalJson(value: unknown): string {
    const sort = (item: unknown): unknown => {
        if (Array.isArray(item)) return item.map(sort);
        if (!item || typeof item !== "object") return item;
        return Object.fromEntries(Object.entries(item).sort(([left], [right]) => left.localeCompare(right)).map(([key, child]) => [key, sort(child)]));
    };
    return JSON.stringify(sort(value));
}
function serverBaseline(project: CanvasProjectInput, version = 1) {
    const { updatedAt: _clientTimestamp, ...content } = project;
    return { source: "server" as const, version, snapshot: canonicalJson(content) };
}

function mockApi(overrides: Partial<ProjectApi> = {}): ProjectApi {
    return {
        list: vi.fn(async () => []),
        create: vi.fn(async (project) => envelope(project)),
        get: vi.fn(async (id) => envelope(projectFor(id))),
        update: vi.fn(async (project, version) => envelope(project, version + 1)),
        remove: vi.fn(async () => undefined),
        ...overrides,
    };
}

afterEach(() => {
    vi.useRealTimers();
    clearCanvasInMemory();
    clearStorageScope();
    setScopedStoreFactoryForTest();
});

it("does not let a late user-A load replace user-B projects", async () => {
    const a = deferred<ProjectEnvelope[]>();
    const api = mockApi({ list: vi.fn().mockReturnValueOnce(a.promise).mockResolvedValueOnce([envelope(projectFor("b"))]) });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    const activationA = sync.activate(captureAppStorageLease()!);

    await setStorageScope({ environment: "test", userId: "user-b" });
    await sync.activate(captureAppStorageLease()!);
    a.resolve([envelope(projectFor("a"))]);
    await activationA;

    expect(useCanvasStore.getState().projects.map((item) => item.id)).toEqual(["b"]);
    sync.stop();
});

it("does not call any project API while the active canvas store is read-only", async () => {
    const api = mockApi();
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "future-user" });
    useCanvasStore.setState({
        loadError: { code: "UNSUPPORTED_GRAPH_SCHEMA", message: "upgrade", readOnly: true },
        projectsLoaded: false,
    });

    await sync.activate(captureAppStorageLease()!);

    expect(api.list).not.toHaveBeenCalled();
    expect(api.create).not.toHaveBeenCalled();
    expect(api.update).not.toHaveBeenCalled();
    expect(api.remove).not.toHaveBeenCalled();
    expect(useCanvasStore.getState().projectsLoaded).toBe(true);
    sync.stop();
});

it("marks projects loaded only after the authoritative server list arrives", async () => {
    const pending = deferred<ProjectEnvelope[]>();
    const sync = new ProjectSync(mockApi({ list: vi.fn(() => pending.promise) }), useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    const activation = sync.activate(captureAppStorageLease()!);
    expect(useCanvasStore.getState().projectsLoaded).toBe(false);
    pending.resolve([envelope(projectFor("server-project"))]);
    await activation;
    expect(useCanvasStore.getState().projectsLoaded).toBe(true);
    sync.stop();
});

it("keeps the remote project and creates a local conflict copy when both devices changed from the same baseline", async () => {
    vi.useFakeTimers();
    const base = projectFor("shared", "共同基线", "2026-08-10T00:00:00.000Z");
    const local = projectFor("shared", "设备 A 修改", "2026-08-10T00:00:20.000Z");
    const remote = projectFor("shared", "设备 B 修改", "2026-08-10T00:00:10.000Z");
    const api = mockApi({ list: vi.fn(async () => [envelope(remote, 2)]) });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    useCanvasStore.setState({
        projects: [local],
        projectSyncMetadata: { shared: serverBaseline(base, 1) },
    } as never);

    await sync.activate(captureAppStorageLease()!);

    expect(useCanvasStore.getState().openProject("shared")?.title).toBe("设备 B 修改");
    const conflictCopy = useCanvasStore.getState().projects.find((project) => project.id !== "shared");
    expect(conflictCopy).toMatchObject({ title: "设备 A 修改（冲突副本）" });
    await vi.advanceTimersByTimeAsync(400);
    expect(api.update).not.toHaveBeenCalledWith(expect.objectContaining({ id: "shared", title: "设备 A 修改" }), expect.anything(), expect.anything());
    expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ id: conflictCopy?.id, title: "设备 A 修改（冲突副本）" }), expect.any(AbortSignal));
    sync.stop();
});

it("uses the authoritative remote refresh when a clean cached project has an untrustworthy newer client timestamp", async () => {
    const base = projectFor("shared", "共同基线", "2026-08-10T00:00:00.000Z");
    const cached = { ...base, updatedAt: "2026-08-10T00:00:30.000Z" };
    const remote = projectFor("shared", "远端刷新", "2026-08-10T00:00:10.000Z");
    const sync = new ProjectSync(mockApi({ list: vi.fn(async () => [envelope(remote, 2)]) }), useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    useCanvasStore.setState({
        projects: [cached],
        projectSyncMetadata: { shared: serverBaseline(base, 1) },
    } as never);

    await sync.activate(captureAppStorageLease()!);

    expect(useCanvasStore.getState().projects).toEqual([{ ...remote, graphSchemaVersion: GRAPH_SCHEMA_VERSION }]);
    sync.stop();
});

it("saves a legacy server project once in canonical graph form without changing its timestamps", async () => {
    vi.useFakeTimers();
    const { graphSchemaVersion: _version, ...remote } = projectFor("legacy-server", "旧服务端画布", "2026-08-10T00:00:10.000Z");
    const api = mockApi({ list: vi.fn(async () => [envelope(remote, 7)]) });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });

    await sync.activate(captureAppStorageLease()!);
    await vi.advanceTimersByTimeAsync(400);

    expect(api.update).toHaveBeenCalledTimes(1);
    expect(api.update).toHaveBeenCalledWith(
        expect.objectContaining({
            id: "legacy-server",
            createdAt: "2026-08-10T00:00:10.000Z",
            updatedAt: "2026-08-10T00:00:10.000Z",
            graphSchemaVersion: GRAPH_SCHEMA_VERSION,
        }),
        7,
        expect.any(AbortSignal),
    );
    sync.stop();
});

it("canonicalizes a persisted server baseline so identical legacy local and remote projects do not create a conflict copy", async () => {
    vi.useFakeTimers();
    const { graphSchemaVersion: _version, ...legacyBase } = projectFor("legacy-baseline", "旧服务端画布", "2026-08-10T00:00:10.000Z");
    const remote = {
        ...legacyBase,
        nodes: [{ id: "prompt", type: "text" as const, title: "Prompt", position: { x: 0, y: 0 }, width: 100, height: 100, metadata: { content: "same" } }],
    };
    const migrated = migrateCanvasPersistedState({
        projects: [remote],
        projectSyncMetadata: { "legacy-baseline": serverBaseline(remote, 4) },
    }, 1);
    useCanvasStore.setState({ projects: migrated.projects, projectSyncMetadata: migrated.projectSyncMetadata } as never);
    const api = mockApi({ list: vi.fn(async () => [envelope(remote, 4)]) });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });

    await sync.activate(captureAppStorageLease()!);
    await vi.advanceTimersByTimeAsync(400);

    expect(useCanvasStore.getState().projects).toHaveLength(1);
    expect(useCanvasStore.getState().projects[0]).toMatchObject({ id: "legacy-baseline", title: "旧服务端画布", graphSchemaVersion: GRAPH_SCHEMA_VERSION });
    expect(useCanvasStore.getState().syncNotice).toBeNull();
    expect(api.update).toHaveBeenCalledTimes(1);
    expect(api.update).toHaveBeenCalledWith(expect.objectContaining({ id: "legacy-baseline", graphSchemaVersion: GRAPH_SCHEMA_VERSION }), 4, expect.any(AbortSignal));
    sync.stop();
});

it("does not resave an already-canonical graph only because JSON keys arrived in another order", async () => {
    vi.useFakeTimers();
    const remote: CanvasProject = {
        ...projectFor("canonical-server"),
        graphSchemaVersion: GRAPH_SCHEMA_VERSION,
        nodes: [
            { id: "prompt", type: "text", title: "Prompt", position: { x: 0, y: 0 }, width: 100, height: 100, metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "hello", outputPortId: "prompt" } } },
            { id: "model", type: "config", title: "Model", position: { x: 200, y: 0 }, width: 100, height: 100, metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: "model", operation: "image.generate", inputPorts: [{ id: "prompt", accepts: "prompt" }], outputPortId: "result", parameters: {} } } },
        ],
        connections: [{ id: "edge", fromNodeId: "prompt", toNodeId: "model", fromPortId: "prompt", toPortId: "prompt" }],
    };
    const api = mockApi({ list: vi.fn(async () => [envelope(remote, 3)]) });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });

    await sync.activate(captureAppStorageLease()!);
    await vi.advanceTimersByTimeAsync(400);

    expect(api.update).not.toHaveBeenCalled();
    sync.stop();
});

it("treats server-sorted JSON as the same baseline and saves a local-only edit with the baseline version", async () => {
    vi.useFakeTimers();
    let serverProject: CanvasProject | null = null;
    const api = mockApi({
        list: vi.fn(async () => serverProject ? [envelope(serverProject, 1)] : []),
        create: vi.fn(async (project) => {
            serverProject = JSON.parse(canonicalJson(project)) as CanvasProject;
            return envelope(serverProject, 1);
        }),
    });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    const lease = captureAppStorageLease()!;
    await sync.activate(lease);
    const id = useCanvasStore.getState().createProject("共同基线");
    await vi.advanceTimersByTimeAsync(400);
    useCanvasStore.getState().renameProject(id, "仅本地修改");

    await sync.activate(lease);
    expect(useCanvasStore.getState().projects).toEqual([expect.objectContaining({ id, title: "仅本地修改" })]);
    await vi.advanceTimersByTimeAsync(400);
    expect(api.update).toHaveBeenCalledWith(expect.objectContaining({ id, title: "仅本地修改" }), 1, expect.any(AbortSignal));
    expect(useCanvasStore.getState().projects).toHaveLength(1);
    sync.stop();
});

it("removes a clean previously synced cache entry missing from the authoritative list without recreating it", async () => {
    vi.useFakeTimers();
    const cached = projectFor("deleted-elsewhere", "已在其他设备删除");
    const api = mockApi({ list: vi.fn(async () => []) });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    useCanvasStore.setState({
        projects: [cached],
        projectSyncMetadata: { [cached.id]: serverBaseline(cached, 4) },
    } as never);

    await sync.activate(captureAppStorageLease()!);
    await vi.advanceTimersByTimeAsync(400);

    expect(useCanvasStore.getState().projects).toEqual([]);
    expect(api.create).not.toHaveBeenCalled();
    sync.stop();
});

it("migrates an old cache into an isolated recovery draft instead of reviving its original missing id", async () => {
    vi.useFakeTimers();
    const legacy = projectFor("legacy-id", "旧缓存草稿");
    const stored = JSON.stringify({ state: { projects: [legacy] }, version: 0 });
    setScopedStoreFactoryForTest(() => ({
        getItem: async () => stored,
        setItem: async (_key: string, value: unknown) => value,
        removeItem: async () => undefined,
        iterate: async () => undefined,
    }) as never);
    await setStorageScope({ environment: "test", userId: "legacy-user" });
    await useCanvasStore.persist.rehydrate();
    const api = mockApi({ list: vi.fn(async () => []) });
    const sync = new ProjectSync(api, useCanvasStore);

    await sync.activate(captureAppStorageLease()!);
    const [recovered] = useCanvasStore.getState().projects;
    expect(recovered).toMatchObject({ title: "旧缓存草稿（本地恢复副本）" });
    expect(recovered.id).not.toBe("legacy-id");
    await vi.advanceTimersByTimeAsync(400);
    expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ id: recovered.id }), expect.any(AbortSignal));
    expect(api.create).not.toHaveBeenCalledWith(expect.objectContaining({ id: "legacy-id" }), expect.anything());
    sync.stop();
});

it("debounces a create and then saves the returned version", async () => {
    vi.useFakeTimers();
    const api = mockApi();
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    await sync.activate(captureAppStorageLease()!);

    const id = useCanvasStore.getState().createProject("新项目");
    await vi.advanceTimersByTimeAsync(400);
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ id, title: "新项目" }), expect.any(AbortSignal));

    useCanvasStore.getState().renameProject(id, "改名项目");
    await vi.advanceTimersByTimeAsync(400);
    expect(api.update).toHaveBeenCalledWith(expect.objectContaining({ id, title: "改名项目" }), 1, expect.any(AbortSignal));
    sync.stop();
});

it("saves only the final viewport after rapid viewport updates", async () => {
    vi.useFakeTimers();
    const api = mockApi();
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    await sync.activate(captureAppStorageLease()!);
    const id = useCanvasStore.getState().createProject("Viewport project");
    await vi.advanceTimersByTimeAsync(400);
    vi.mocked(api.create).mockClear();

    useCanvasStore.getState().updateProject(id, { viewport: { x: 20, y: -10, k: 1.1 } });
    useCanvasStore.getState().updateProject(id, { viewport: { x: 50, y: -25, k: 1.25 } });
    useCanvasStore.getState().updateProject(id, { viewport: { x: 120, y: -45, k: 1.75 } });

    await vi.advanceTimersByTimeAsync(400);

    expect(api.update).toHaveBeenCalledTimes(1);
    expect(api.update).toHaveBeenCalledWith(
        expect.objectContaining({ id, viewport: { x: 120, y: -45, k: 1.75 } }),
        1,
        expect.any(AbortSignal),
    );
    sync.stop();
});

it("keeps a failed viewport change and retries the latest project on the next edit", async () => {
    vi.useFakeTimers();
    const api = mockApi({
        list: vi.fn(async () => [envelope(projectFor("p-1"), 1)]),
        update: vi.fn().mockRejectedValueOnce(new TypeError("offline")).mockImplementation(async (project, version) => envelope(project, version + 1)),
    });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    await sync.activate(captureAppStorageLease()!);

    useCanvasStore.getState().updateProject("p-1", { viewport: { x: 90, y: 40, k: 1.5 } });
    await vi.advanceTimersByTimeAsync(400);
    expect(useCanvasStore.getState().openProject("p-1")?.viewport).toEqual({ x: 90, y: 40, k: 1.5 });
    expect(useCanvasStore.getState().syncNotice).toBe("项目暂时无法同步，当前修改仍保留在本机。");

    useCanvasStore.getState().renameProject("p-1", "retry save");
    await vi.advanceTimersByTimeAsync(400);
    expect(api.update).toHaveBeenLastCalledWith(
        expect.objectContaining({ title: "retry save", viewport: { x: 90, y: 40, k: 1.5 } }),
        1,
        expect.any(AbortSignal),
    );
    expect(useCanvasStore.getState().syncNotice).toBe("项目已恢复同步。");
    useCanvasStore.getState().renameProject("p-1", "同步稳定");
    await vi.advanceTimersByTimeAsync(400);
    expect(useCanvasStore.getState().syncNotice).toBeNull();
    sync.stop();
});

it("isolates local work and removes the original route when PUT reports PROJECT_NOT_FOUND", async () => {
    vi.useFakeTimers();
    const missing = Object.assign(new Error("missing"), { code: "PROJECT_NOT_FOUND" });
    const api = mockApi({
        list: vi.fn(async () => [envelope(projectFor("p-1", "初始"), 1)]),
        update: vi.fn(async () => { throw missing; }),
    });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    await sync.activate(captureAppStorageLease()!);

    useCanvasStore.getState().renameProject("p-1", "本地未保存修改");
    await vi.advanceTimersByTimeAsync(400);

    expect(useCanvasStore.getState().openProject("p-1")).toBeNull();
    expect(useCanvasStore.getState().projects).toEqual([
        expect.objectContaining({ title: "本地未保存修改（本地恢复副本）" }),
    ]);
    expect(useCanvasStore.getState().projectsLoaded).toBe(true);
    expect(useCanvasStore.getState().syncNotice).toBe("原画布已删除或无法访问，本地修改已另存为恢复副本。");
    sync.stop();
});

it("isolates local work and removes the original route when conflict recovery GET reports PROJECT_NOT_FOUND", async () => {
    vi.useFakeTimers();
    const conflict = Object.assign(new Error("conflict"), { code: "PROJECT_CONFLICT" });
    const missing = Object.assign(new Error("missing"), { code: "PROJECT_NOT_FOUND" });
    const api = mockApi({
        list: vi.fn(async () => [envelope(projectFor("p-1", "初始"), 1)]),
        update: vi.fn(async () => { throw conflict; }),
        get: vi.fn(async () => { throw missing; }),
    });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    await sync.activate(captureAppStorageLease()!);

    useCanvasStore.getState().renameProject("p-1", "冲突期间修改");
    await vi.advanceTimersByTimeAsync(400);

    expect(useCanvasStore.getState().openProject("p-1")).toBeNull();
    expect(useCanvasStore.getState().projects).toEqual([
        expect.objectContaining({ title: "冲突期间修改（本地恢复副本）" }),
    ]);
    expect(useCanvasStore.getState().syncNotice).toBe("原画布已删除或无法访问，本地修改已另存为恢复副本。");
    sync.stop();
});

it("normalizes hostile, incomplete, and over-500-percent imported viewports", () => {
    const hostileId = useCanvasStore.getState().importProject({ viewport: { x: Number.NaN, y: Number.POSITIVE_INFINITY, k: -1 } });
    const incompleteId = useCanvasStore.getState().importProject({ viewport: { x: 20 } as CanvasProject["viewport"] });
    const oversizedId = useCanvasStore.getState().importProject({ viewport: { x: 30, y: -15, k: 9 } });

    expect(useCanvasStore.getState().openProject(hostileId)?.viewport).toEqual({ x: 0, y: 0, k: 1 });
    expect(useCanvasStore.getState().openProject(incompleteId)?.viewport).toEqual({ x: 0, y: 0, k: 1 });
    expect(useCanvasStore.getState().openProject(oversizedId)?.viewport).toEqual({ x: 30, y: -15, k: 5 });
});

it("does not apply a completed user-A save after user-B activates", async () => {
    vi.useFakeTimers();
    const pendingCreate = deferred<ProjectEnvelope>();
    const api = mockApi({ create: vi.fn(() => pendingCreate.promise) });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    await sync.activate(captureAppStorageLease()!);
    const aProject = projectFor("a", "A only");
    useCanvasStore.getState().replaceProjects([aProject]);
    await vi.advanceTimersByTimeAsync(400);

    await setStorageScope({ environment: "test", userId: "user-b" });
    useCanvasStore.getState().replaceProjects([]);
    await sync.activate(captureAppStorageLease()!);
    pendingCreate.resolve(envelope(aProject));
    await Promise.resolve();

    expect(useCanvasStore.getState().projects).toEqual([]);
    sync.stop();
});

it("preserves a local conflict copy instead of overwriting it", async () => {
    vi.useFakeTimers();
    const server = projectFor("p-1", "服务端版本", "2026-08-10T00:00:01.000Z");
    const conflict = Object.assign(new Error("conflict"), { code: "PROJECT_CONFLICT" });
    const api = mockApi({
        list: vi.fn(async () => [envelope(projectFor("p-1", "初始版本"), 2)]),
        update: vi.fn(async () => { throw conflict; }),
        get: vi.fn(async () => envelope(server, 3)),
    });
    const sync = new ProjectSync(api, useCanvasStore);
    await setStorageScope({ environment: "test", userId: "user-a" });
    await sync.activate(captureAppStorageLease()!);

    useCanvasStore.getState().renameProject("p-1", "本地修改");
    await vi.advanceTimersByTimeAsync(400);

    expect(useCanvasStore.getState().projects.some((item) => item.id === "p-1" && item.title === "服务端版本")).toBe(true);
    expect(useCanvasStore.getState().projects.some((item) => item.id !== "p-1" && item.title === "本地修改（冲突副本）")).toBe(true);
    expect(useCanvasStore.getState().syncNotice).toBe("检测到其他位置的更新，已保留一个冲突副本。");
    sync.stop();
});
