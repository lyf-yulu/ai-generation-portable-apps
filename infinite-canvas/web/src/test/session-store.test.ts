import { expect, it, vi } from "vitest";

const events: string[] = [];
const session = { user_id: "user-b", username: "B", role: "user" as const };

vi.mock("@/storage/scope", () => ({
    clearStorageScope: vi.fn(() => events.push("scope:clear")),
    setStorageScope: vi.fn(async () => events.push("scope:set")),
    captureScopedStore: vi.fn(() => null),
}));
vi.mock("@/stores/canvas/use-canvas-store", () => ({
    clearCanvasInMemory: () => events.push("canvas:clear"),
    useCanvasStore: {
        getState: () => ({ replaceProjects: () => events.push("canvas:clear") }),
        setState: () => events.push("canvas:reset"),
        persist: { rehydrate: vi.fn(async () => events.push("canvas:load")) },
    },
}));
vi.mock("@/stores/use-asset-store", () => ({
    useAssetStore: {
        getState: () => ({ replaceAssets: () => events.push("assets:clear") }),
        setState: () => events.push("assets:reset"),
        persist: { rehydrate: vi.fn(async () => events.push("assets:load")) },
    },
}));

import { useSessionStore } from "@/stores/portal/use-session-store";

it("clears the old in-memory canvas before loading the next Portal user scope", async () => {
    events.length = 0;
    await useSessionStore.getState().setSession(session, "test");
    expect(events).toEqual(["scope:clear", "canvas:clear", "assets:reset", "scope:set", "canvas:load", "assets:load"]);
    expect(useSessionStore.getState().session).toEqual(session);
});

it("logout releases the active scope and in-memory user state without deleting data", () => {
    events.length = 0;
    useSessionStore.getState().clearSession();
    expect(events).toEqual(["scope:clear", "canvas:clear", "assets:reset"]);
    expect(useSessionStore.getState().session).toBeNull();
});
