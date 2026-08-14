import { afterEach, expect, it, vi } from "vitest";

let resolveThumbnail: ((value: string) => void) | undefined;
vi.mock("@/lib/canvas/canvas-image-data", () => ({ upscaleDataUrl: vi.fn(() => new Promise<string>((resolve) => { resolveThumbnail = resolve; })) }));

import { StorageScopeChangedError, clearStorageScope, setScopedStoreFactoryForTest, setStorageScope } from "@/storage/scope";
import { savePendingAgentUserMessage } from "@/services/agent-chat-storage";

afterEach(() => { clearStorageScope(); setScopedStoreFactoryForTest(); });

it("rejects deferred A agent attachment without writing an A record into B store", async () => {
    const writes = new Map<string, string[]>();
    setScopedStoreFactoryForTest(({ name }) => ({
        getItem: async () => null,
        setItem: async (key: string, value: unknown) => { writes.set(name, [...(writes.get(name) || []), `${key}:${String(value)}`]); return value; },
        removeItem: async () => undefined,
        iterate: async () => undefined,
    }) as never);
    await setStorageScope({ environment: "test", userId: "a" });
    const pending = savePendingAgentUserMessage({ id: "a-msg", role: "user", text: "x", historyText: "x", attachments: [{ id: "a", dataUrl: "data:image/png;base64,AA", url: "", width: 600, height: 600, size: 1 }] } as never);
    await Promise.resolve();
    await Promise.resolve();
    await setStorageScope({ environment: "test", userId: "b" });
    resolveThumbnail!("data:image/png;base64,BB");
    await expect(pending).rejects.toBeInstanceOf(StorageScopeChangedError);
    expect(writes.get("ai-creation-canvas:test:b") || []).toEqual([]);
});
