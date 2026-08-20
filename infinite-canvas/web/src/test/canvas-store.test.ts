import { afterEach, describe, expect, it, vi } from "vitest";

import { GRAPH_SCHEMA_VERSION } from "@/features/graph/contracts";
import type { CanvasProjectInput } from "@/features/graph/normalize-project";
import { CanvasReadOnlyError, clearCanvasInMemory, migrateCanvasPersistedState, useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { clearStorageScope, setScopedStoreFactoryForTest, setStorageScope, storageDatabaseName } from "@/storage/scope";
import { CanvasNodeType } from "@/types/canvas";

const timestamp = "2026-08-11T01:02:03.000Z";

function legacyProject(id = "legacy"): CanvasProjectInput {
    return {
        id,
        title: "Legacy",
        createdAt: timestamp,
        updatedAt: timestamp,
        nodes: [{ id: "text", type: CanvasNodeType.Text, title: "Text", position: { x: 0, y: 0 }, width: 200, height: 100, metadata: { content: "hello" } }],
        connections: [],
        chatSessions: [],
        activeChatId: null,
        backgroundMode: "lines",
        showImageInfo: false,
        viewport: { x: 0, y: 0, k: 1 },
    };
}

afterEach(() => {
    vi.useRealTimers();
    clearCanvasInMemory();
    clearStorageScope();
    setScopedStoreFactoryForTest();
});

describe("canvas graph persistence", () => {
    it("creates graph-versioned projects", () => {
        const id = useCanvasStore.getState().createProject("New");
        expect(useCanvasStore.getState().openProject(id)?.graphSchemaVersion).toBe(GRAPH_SCHEMA_VERSION);
    });

    it("normalizes imported projects without retaining the caller's mutable node arrays", () => {
        const source = legacyProject();
        const id = useCanvasStore.getState().importProject(source);
        source.nodes[0].metadata!.content = "changed outside";

        const imported = useCanvasStore.getState().openProject(id)!;
        expect(imported.graphSchemaVersion).toBe(GRAPH_SCHEMA_VERSION);
        expect(imported.nodes[0].metadata?.graph).toMatchObject({ role: "prompt", text: "hello" });
        expect(imported.nodes[0].metadata?.content).toBe("hello");
    });

    it.each([1, 2])("protects raw future-schema bytes after a version-%i rehydrate failure and resets protection for another user", async (persistedVersion) => {
        vi.useFakeTimers();
        const key = "infinite-canvas:canvas_store";
        const futureProject: CanvasProjectInput = {
            ...legacyProject("future-project"),
            graphSchemaVersion: GRAPH_SCHEMA_VERSION + 1,
            nodes: [],
        };
        const raw = JSON.stringify({ state: { projects: [futureProject], projectSyncMetadata: {} }, version: persistedVersion });
        const values = new Map<string, Map<string, string>>();
        const writes = new Map<string, number>();
        const futureDatabase = storageDatabaseName({ environment: "test", userId: `future-v${persistedVersion}` });
        values.set(futureDatabase, new Map([[key, raw]]));
        setScopedStoreFactoryForTest(({ name }) => ({
            getItem: async (itemKey: string) => values.get(name)?.get(itemKey) ?? null,
            setItem: async (itemKey: string, value: string) => {
                writes.set(name, (writes.get(name) ?? 0) + 1);
                const database = values.get(name) ?? new Map<string, string>();
                database.set(itemKey, value);
                values.set(name, database);
                return value;
            },
            removeItem: async (itemKey: string) => { values.get(name)?.delete(itemKey); },
            iterate: async () => undefined,
        }) as never);

        await setStorageScope({ environment: "test", userId: `future-v${persistedVersion}` });
        await useCanvasStore.persist.rehydrate();
        await vi.advanceTimersByTimeAsync(500);

        expect(values.get(futureDatabase)?.get(key)).toBe(raw);
        expect(writes.get(futureDatabase) ?? 0).toBe(0);
        expect(useCanvasStore.getState().loadError).toMatchObject({ code: "UNSUPPORTED_GRAPH_SCHEMA", message: expect.stringMatching(/更新版本.*升级应用.*只读/), readOnly: true });
        expect(() => useCanvasStore.getState().createProject("must stay memory only")).toThrow(CanvasReadOnlyError);
        await vi.advanceTimersByTimeAsync(500);
        expect(values.get(futureDatabase)?.get(key)).toBe(raw);
        expect(writes.get(futureDatabase) ?? 0).toBe(0);

        clearCanvasInMemory();
        expect(values.get(futureDatabase)?.get(key)).toBe(raw);
        await setStorageScope({ environment: "test", userId: "supported-user" });
        await useCanvasStore.persist.rehydrate();
        useCanvasStore.getState().createProject("supported write");
        await vi.advanceTimersByTimeAsync(500);
        const supportedDatabase = storageDatabaseName({ environment: "test", userId: "supported-user" });
        expect(writes.get(supportedDatabase)).toBeGreaterThan(0);
        expect(values.get(supportedDatabase)?.get(key)).toContain("supported write");
    });

    it.each([
        { version: GRAPH_SCHEMA_VERSION + 1, nodeVersion: undefined },
        { version: GRAPH_SCHEMA_VERSION + 1, nodeVersion: GRAPH_SCHEMA_VERSION },
    ])("rejects imported future project version $version without adding a project", ({ version, nodeVersion }) => {
        const before = useCanvasStore.getState().projects;
        const source: CanvasProjectInput = {
            ...legacyProject("future-import"),
            graphSchemaVersion: version,
            nodes: nodeVersion === undefined ? [] : [{
                id: "prompt",
                type: CanvasNodeType.Text,
                title: "Prompt",
                position: { x: 0, y: 0 },
                width: 100,
                height: 100,
                metadata: { graph: { schemaVersion: nodeVersion, role: "prompt", text: "hello", outputPortId: "prompt" } },
            }],
        };

        expect(() => useCanvasStore.getState().importProject(source)).toThrow("Unsupported canvas graph schema version");
        expect(useCanvasStore.getState().projects).toBe(before);
    });

    it("blocks every public project mutation while future data is read-only", () => {
        const id = useCanvasStore.getState().createProject("Protected");
        const before = useCanvasStore.getState().projects;
        const beforeMetadata = useCanvasStore.getState().projectSyncMetadata;
        useCanvasStore.setState({ loadError: { code: "UNSUPPORTED_GRAPH_SCHEMA", message: "upgrade", readOnly: true } });

        expect(() => useCanvasStore.getState().createProject("Blocked create")).toThrow(CanvasReadOnlyError);
        expect(() => useCanvasStore.getState().importProject(legacyProject("blocked-import"))).toThrow(CanvasReadOnlyError);
        useCanvasStore.getState().renameProject(id, "Blocked rename");
        useCanvasStore.getState().updateProject(id, { nodes: [] });
        useCanvasStore.getState().deleteProjects([id]);
        useCanvasStore.getState().replaceProjects([]);
        useCanvasStore.getState().setProjectSyncMetadata(id, { source: "draft" });

        expect(useCanvasStore.getState().projects).toBe(before);
        expect(useCanvasStore.getState().projectSyncMetadata).toBe(beforeMetadata);
    });

    it("normalizes authoritative server replacements while preserving server IDs and timestamps", () => {
        const source = legacyProject("server-id");
        useCanvasStore.getState().replaceProjects([source]);

        const stored = useCanvasStore.getState().openProject("server-id")!;
        expect(stored).toMatchObject({ id: "server-id", createdAt: timestamp, updatedAt: timestamp, graphSchemaVersion: GRAPH_SCHEMA_VERSION });
        expect(stored.nodes[0].metadata?.graph?.role).toBe("prompt");
    });

    it("sanitizes node scales when normalizing stored projects", () => {
        const source: CanvasProjectInput = {
            ...legacyProject("scaled"),
            nodes: [
                { id: "ok", type: CanvasNodeType.Text, title: "OK", position: { x: 0, y: 0 }, width: 200, height: 100, scale: 0.5 },
                { id: "high", type: CanvasNodeType.Text, title: "High", position: { x: 0, y: 0 }, width: 200, height: 100, scale: 99 },
                { id: "low", type: CanvasNodeType.Text, title: "Low", position: { x: 0, y: 0 }, width: 200, height: 100, scale: 0.01 },
                { id: "bad", type: CanvasNodeType.Text, title: "Bad", position: { x: 0, y: 0 }, width: 200, height: 100, scale: "abc" as unknown as number },
                { id: "plain", type: CanvasNodeType.Text, title: "Plain", position: { x: 0, y: 0 }, width: 200, height: 100 },
            ],
        };
        useCanvasStore.getState().replaceProjects([source]);

        const nodes = useCanvasStore.getState().openProject("scaled")!.nodes;
        expect(nodes.find((node) => node.id === "ok")?.scale).toBe(0.5);
        expect(nodes.find((node) => node.id === "high")?.scale).toBe(4);
        expect(nodes.find((node) => node.id === "low")?.scale).toBe(0.25);
        expect(nodes.find((node) => node.id === "bad")?.scale).toBeUndefined();
        expect(nodes.find((node) => node.id === "plain")?.scale).toBeUndefined();
    });

    it("normalizes old persisted projects and marks their sync metadata as legacy", () => {
        const source = legacyProject();
        const migrated = migrateCanvasPersistedState({ projects: [source] }, 0);

        expect(migrated.projects[0]).toMatchObject({ id: "legacy", updatedAt: timestamp, graphSchemaVersion: GRAPH_SCHEMA_VERSION });
        expect(migrated.projects[0].nodes[0].metadata?.graph?.role).toBe("prompt");
        expect(migrated.projectSyncMetadata).toEqual({ legacy: { source: "legacy" } });
    });

    it("normalizes version-one caches even when sync metadata already exists", () => {
        const source = legacyProject();
        const migrated = migrateCanvasPersistedState({ projects: [source], projectSyncMetadata: { legacy: { source: "draft" } } }, 1);

        expect(migrated.projects[0].graphSchemaVersion).toBe(GRAPH_SCHEMA_VERSION);
        expect(migrated.projectSyncMetadata).toEqual({ legacy: { source: "draft" } });
    });
});
