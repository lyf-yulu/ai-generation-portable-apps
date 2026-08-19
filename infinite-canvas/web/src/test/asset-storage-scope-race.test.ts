import { afterEach, expect, it } from "vitest";

import { clearStorageScope, setScopedStoreFactoryForTest, setStorageScope } from "@/storage/scope";
import { useAssetStore } from "@/stores/use-asset-store";

afterEach(() => { useAssetStore.setState({ assets: [], hydrated: false }); clearStorageScope(); setScopedStoreFactoryForTest(); });

it("does not let a deferred A asset rehydrate overwrite B assets", async () => {
    let resolveA: (value: string | null) => void = () => undefined;
    const deferredA = new Promise<string | null>((resolve) => { resolveA = resolve; });
    const bState = JSON.stringify({ state: { assets: [{ id: "b", kind: "text", title: "B", coverUrl: "", tags: [], createdAt: "", updatedAt: "", data: { content: "B" } }] }, version: 0 });
    setScopedStoreFactoryForTest(({ name }) => ({
        getItem: async () => name.endsWith(":a") ? deferredA : bState,
        setItem: async (_key: string, value: unknown) => value,
        removeItem: async () => undefined,
        iterate: async () => undefined,
    }) as never);
    await setStorageScope({ environment: "test", userId: "a" });
    const a = useAssetStore.persist.rehydrate();
    await setStorageScope({ environment: "test", userId: "b" });
    await useAssetStore.persist.rehydrate();
    resolveA(JSON.stringify({ state: { assets: [{ id: "a", kind: "text", title: "A", coverUrl: "", tags: [], createdAt: "", updatedAt: "", data: { content: "A" } }] }, version: 0 }));
    await a;
    expect(useAssetStore.getState().assets.map((asset) => asset.id)).toEqual(["b"]);
});
