import { nanoid } from "nanoid";
import { readImageMeta } from "@/lib/image-utils";
import { captureScopedStore, isStorageLeaseActive, onStorageScopeCleared, StorageScopeChangedError, type ScopedStoreLease } from "@/storage/scope";

export type UploadedImage = {
    url: string;
    storageKey: string;
    width: number;
    height: number;
    bytes: number;
    mimeType: string;
};

const objectUrls = new Map<string, string>();

function lease() {
    const captured = captureScopedStore("image_files");
    if (!captured) throw new Error("A Portal session is required before accessing image storage");
    return captured;
}
function assertActive(captured: ScopedStoreLease) {
    if (!isStorageLeaseActive(captured)) throw new StorageScopeChangedError();
}

onStorageScopeCleared(() => {
    objectUrls.forEach((url) => URL.revokeObjectURL(url));
    objectUrls.clear();
});

export async function uploadImage(input: string | Blob): Promise<UploadedImage> {
    const captured = lease();
    const blob = typeof input === "string" ? await (await fetch(input)).blob() : input;
    assertActive(captured);
    const storageKey = `image:${nanoid()}`;
    await captured.store.setItem(storageKey, blob);
    assertActive(captured);
    const url = URL.createObjectURL(blob);
    const meta = await readImageMeta(url);
    if (!isStorageLeaseActive(captured)) {
        URL.revokeObjectURL(url);
        throw new StorageScopeChangedError();
    }
    objectUrls.set(storageKey, url);
    return { url, storageKey, width: meta.width, height: meta.height, bytes: blob.size, mimeType: blob.type || meta.mimeType };
}

export async function resolveImageUrl(storageKey?: string, fallback = "") {
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

export async function getImageBlob(storageKey: string) {
    const captured = lease();
    const blob = await captured.store.getItem<Blob>(storageKey);
    assertActive(captured);
    return blob;
}

export async function setImageBlob(storageKey: string, blob: Blob) {
    const captured = lease();
    await captured.store.setItem(storageKey, blob);
    assertActive(captured);
    const url = URL.createObjectURL(blob);
    objectUrls.set(storageKey, url);
    return url;
}

export async function imageToDataUrl(image: { url?: string; dataUrl?: string; storageKey?: string }) {
    const url = image.dataUrl || (await resolveImageUrl(image.storageKey, image.url || ""));
    if (!url || url.startsWith("data:")) return url;
    return blobToDataUrl(await (await fetch(url)).blob());
}

export async function deleteStoredImages(keys: Iterable<string>) {
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

export async function cleanupUnusedImages(usedData: unknown) {
    const captured = lease();
    const usedKeys = collectImageStorageKeys(usedData);
    const unused: string[] = [];
    await captured.store.iterate<unknown, void>((_value: unknown, key: string) => {
        if (!usedKeys.has(key)) unused.push(key);
    });
    assertActive(captured);
    await deleteStoredImages(unused);
}

export function collectImageStorageKeys(value: unknown, keys = new Set<string>()) {
    if (!value || typeof value !== "object") return keys;
    if ("storageKey" in value && typeof value.storageKey === "string" && value.storageKey.startsWith("image:")) keys.add(value.storageKey);
    Object.values(value).forEach((item) => (Array.isArray(item) ? item.forEach((child) => collectImageStorageKeys(child, keys)) : collectImageStorageKeys(item, keys)));
    return keys;
}

function blobToDataUrl(blob: Blob) {
    return new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(new Error("读取图片失败"));
        reader.readAsDataURL(blob);
    });
}
