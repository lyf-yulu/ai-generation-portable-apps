import { afterEach, expect, it } from "vitest";

import { captureScopedStore, clearStorageScope, currentStorageScope, isStorageLeaseActive, setStorageScope, storageDatabaseName } from "@/storage/scope";

afterEach(() => clearStorageScope());

it("separates users and environments", async () => {
    expect(storageDatabaseName({ environment: "test", userId: "u-a" })).toBe("ai-creation-canvas:test:u-a");
    expect(storageDatabaseName({ environment: "test", userId: "u-b" })).not.toBe(storageDatabaseName({ environment: "test", userId: "u-a" }));
    expect(storageDatabaseName({ environment: "production", userId: "u-a" })).not.toBe(storageDatabaseName({ environment: "test", userId: "u-a" }));
});

it("invalidates a captured storage lease when another user becomes active", async () => {
    await setStorageScope({ environment: "test", userId: "user-a" });
    const lease = captureScopedStore("app_state");
    expect(isStorageLeaseActive(lease!)).toBe(true);
    await setStorageScope({ environment: "test", userId: "user-b" });
    expect(isStorageLeaseActive(lease!)).toBe(false);
});

it("encodes scope segments so separators and percent characters cannot collide", () => {
    expect(storageDatabaseName({ environment: "a:b", userId: "c" })).not.toBe(storageDatabaseName({ environment: "a", userId: "b:c" }));
    expect(storageDatabaseName({ environment: "test", userId: "a:b" })).not.toBe(storageDatabaseName({ environment: "test", userId: "a%3Ab" }));
});

it("does not expose a storage scope until a Portal user is established", async () => {
    expect(currentStorageScope()).toBeNull();
    await setStorageScope({ environment: "test", userId: "portal-user" });
    expect(currentStorageScope()).toEqual({ environment: "test", userId: "portal-user" });
    clearStorageScope();
    expect(currentStorageScope()).toBeNull();
});
