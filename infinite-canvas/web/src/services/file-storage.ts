import { nanoid } from "nanoid";
import { captureScopedStore, isStorageLeaseActive, onStorageScopeCleared, StorageScopeChangedError, type ScopedStoreLease } from "@/storage/scope";

export type UploadedFile = { url: string; storageKey: string; bytes: number; mimeType: string; width?: number; height?: number; durationMs?: number };

const objectUrls = new Map<string, string>();

function lease() {
    const captured = captureScopedStore("media_files");
    if (!captured) throw new Error("A Portal session is required before accessing media storage");
    return captured;
}
function assertActive(captured: ScopedStoreLease) {
    if (!isStorageLeaseActive(captured)) throw new StorageScopeChangedError();
}

onStorageScopeCleared(() => {
    objectUrls.forEach((url) => URL.revokeObjectURL(url));
    objectUrls.clear();
});

export async function uploadMediaFile(input: string | Blob, prefix = "file"): Promise<UploadedFile> {
    const captured = lease();
    const blob = typeof input === "string" ? await (await fetch(input)).blob() : input;
    assertActive(captured);
    const storageKey = `${prefix}:${nanoid()}`;
    await captured.store.setItem(storageKey, blob);
    assertActive(captured);
    const url = URL.createObjectURL(blob);
    const meta = blob.type.startsWith("video/") ? await readVideoMeta(url) : blob.type.startsWith("audio/") ? await readAudioMeta(url) : {};
    if (!isStorageLeaseActive(captured)) {
        URL.revokeObjectURL(url);
        throw new StorageScopeChangedError();
    }
    objectUrls.set(storageKey, url);
    return { url, storageKey, bytes: blob.size, mimeType: blob.type || "application/octet-stream", ...meta };
}

export async function resolveMediaUrl(storageKey?: string, fallback = "") {
    if (!storageKey) return fallback;
    const captured = lease();
    const cached = objectUrls.get(storageKey);
    if (cached) return cached;
    const blob = await captured.store.getItem<Blob>(storageKey);
    assertActive(captured);
    if (!blob) return fallback;
    const url = URL.createObjectURL(blob);
    objectUrls.set(storageKey, url);
    return url;
}

export async function getMediaBlob(storageKey: string) {
    const captured = lease();
    const blob = await captured.store.getItem<Blob>(storageKey);
    assertActive(captured);
    return blob;
}

export async function setMediaBlob(storageKey: string, blob: Blob) {
    const captured = lease();
    await captured.store.setItem(storageKey, blob);
    assertActive(captured);
    const url = URL.createObjectURL(blob);
    objectUrls.set(storageKey, url);
    return url;
}

export async function deleteStoredMedia(keys: Iterable<string>) {
    const captured = lease();
    await Promise.all(
        Array.from(new Set(keys)).map(async (key) => {
            const url = objectUrls.get(key);
            if (url) URL.revokeObjectURL(url);
            objectUrls.delete(key);
            assertActive(captured);
            await captured.store.removeItem(key);
            assertActive(captured);
        }),
    );
}

export async function cleanupUnusedMedia(usedData: unknown) {
    const captured = lease();
    const usedKeys = collectMediaStorageKeys(usedData);
    const unused: string[] = [];
    await captured.store.iterate<unknown, void>((_value: unknown, key: string) => {
        if (!usedKeys.has(key)) unused.push(key);
    });
    assertActive(captured);
    await Promise.all(unused.map(async (key) => {
        await captured.store.removeItem(key);
        assertActive(captured);
    }));
}

export function collectMediaStorageKeys(value: unknown, keys = new Set<string>()) {
    if (!value || typeof value !== "object") return keys;
    if ("storageKey" in value && typeof value.storageKey === "string" && value.storageKey.includes(":")) keys.add(value.storageKey);
    Object.values(value).forEach((item) => (Array.isArray(item) ? item.forEach((child) => collectMediaStorageKeys(child, keys)) : collectMediaStorageKeys(item, keys)));
    return keys;
}

function readVideoMeta(url: string) {
    return new Promise<{ width: number; height: number; durationMs?: number }>((resolve) => {
        const video = document.createElement("video");
        const done = () => resolve({ width: video.videoWidth || 1280, height: video.videoHeight || 720, durationMs: Number.isFinite(video.duration) ? Math.round(video.duration * 1000) : undefined });
        video.onloadedmetadata = done;
        video.onerror = done;
        video.src = url;
    });
}

function readAudioMeta(url: string) {
    return new Promise<{ durationMs?: number }>((resolve) => {
        const audio = document.createElement("audio");
        const done = () => resolve({ durationMs: Number.isFinite(audio.duration) ? Math.round(audio.duration * 1000) : undefined });
        audio.onloadedmetadata = done;
        audio.onerror = done;
        audio.src = url;
    });
}
