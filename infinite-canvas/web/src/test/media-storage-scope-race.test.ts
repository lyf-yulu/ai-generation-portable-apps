import { afterEach, expect, it, vi } from "vitest";

import { StorageScopeChangedError, clearStorageScope, setStorageScope } from "@/storage/scope";
import { uploadImage } from "@/services/image-storage";

afterEach(() => {
    vi.unstubAllGlobals();
    clearStorageScope();
});

it("rejects an image upload started by A after B becomes the active scope", async () => {
    let finishResponse: ((response: Response) => void) | undefined;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { finishResponse = resolve; })));
    await setStorageScope({ environment: "test", userId: "user-a" });
    const upload = uploadImage("/pending-image");
    await setStorageScope({ environment: "test", userId: "user-b" });
    finishResponse!(new Response(new Blob(["image"], { type: "image/png" })));
    await expect(upload).rejects.toBeInstanceOf(StorageScopeChangedError);
});
