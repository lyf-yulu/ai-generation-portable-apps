import { afterEach, expect, it, vi } from "vitest";

import { clearCanvasInMemory, useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { clearStorageScope, setScopedStoreFactoryForTest, setStorageScope } from "@/storage/scope";

afterEach(() => {
    vi.useRealTimers();
    clearCanvasInMemory();
    clearStorageScope();
});

it("cancels a queued canvas write before a user switch or logout", async () => {
    vi.useFakeTimers();
    await setStorageScope({ environment: "test", userId: "user-a" });
    useCanvasStore.getState().createProject("A only");
    clearCanvasInMemory();
    await setStorageScope({ environment: "test", userId: "user-b" });
    await vi.advanceTimersByTimeAsync(500);
    expect(useCanvasStore.getState().projects).toEqual([]);

    await setStorageScope({ environment: "test", userId: "user-a" });
    useCanvasStore.getState().createProject("logout only");
    clearCanvasInMemory();
    clearStorageScope();
    await vi.advanceTimersByTimeAsync(500);
    expect(useCanvasStore.getState().projects).toEqual([]);
});

it("does not write A's queued canvas snapshot into B's persistent store", async () => {
    const writes = new Map<string, string[]>();
    setScopedStoreFactoryForTest(({ name }) => ({
        getItem: async () => null,
        setItem: async (key: string, value: string) => { writes.set(name, [...(writes.get(name) || []), `${key}:${value}`]); return value; },
        removeItem: async () => undefined,
        iterate: async () => undefined,
    }) as never);
    vi.useFakeTimers();
    await setStorageScope({ environment: "test", userId: "a" });
    useCanvasStore.getState().createProject("A");
    await setStorageScope({ environment: "test", userId: "b" });
    await vi.advanceTimersByTimeAsync(500);
    expect(writes.get("ai-creation-canvas:test:b") || []).toEqual([]);
});
