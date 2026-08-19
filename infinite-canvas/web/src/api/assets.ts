import { apiFetch, assetUrl, csrfTokenForRequest, safeApiPath } from "./client";
import type { AssetRef, OwnedMediaAsset } from "./contracts";
import type { GraphMediaType } from "@/features/graph/contracts";

type UploadResponse = {
    asset_id?: unknown;
    kind?: unknown;
    status?: unknown;
    media_type?: unknown;
    mime_type?: unknown;
    size_bytes?: unknown;
};

function assetFromResponse(value: unknown): AssetRef {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("媒体上传响应无效，请重试。");
    const response = value as UploadResponse;
    const id = response.asset_id;
    if (typeof id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(id)
        || (response.kind !== "reference" && response.kind !== "portrait" && response.kind !== "library")
        || (response.status !== "processing" && response.status !== "active" && response.status !== "failed")
        || (response.media_type !== "image" && response.media_type !== "video" && response.media_type !== "audio")
        || typeof response.mime_type !== "string" || !response.mime_type.startsWith(`${response.media_type}/`)
        || typeof response.size_bytes !== "number" || !Number.isSafeInteger(response.size_bytes) || response.size_bytes < 1) {
        throw new Error("媒体上传响应无效，请重试。");
    }
    return {
        id,
        kind: response.kind,
        status: response.status,
        media_type: response.media_type,
        mime_type: response.mime_type,
        size_bytes: response.size_bytes,
        content_url: safeApiPath(`${assetUrl(id)}/content`),
    };
}

function ownedAssetFromResponse(value: unknown, expectedMediaType: GraphMediaType): OwnedMediaAsset {
    const asset = assetFromResponse(value);
    if (asset.kind !== "reference" || asset.status !== "active" || asset.media_type !== expectedMediaType
        || typeof asset.size_bytes !== "number" || typeof asset.content_url !== "string") {
        throw new Error("媒体上传响应无效，请重试。");
    }
    return asset as OwnedMediaAsset;
}

export async function fetchAsset(id: string) {
    const response = await apiFetch<unknown>(`/api/v1/assets/${encodeURIComponent(id)}`);
    return assetFromResponse(response);
}

export async function deleteMediaAsset(id: string): Promise<void> {
    await apiFetch<void>(assetUrl(id), { method: "DELETE" });
}

function uploadAsset(
    file: File,
    mediaType: GraphMediaType,
    kind: "reference" | "library",
    onProgress: (percent: number) => void,
    signal?: AbortSignal,
): Promise<AssetRef> {
    if (signal?.aborted) return Promise.reject(new DOMException("The upload was cancelled.", "AbortError"));
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        const abortRequest = () => request.abort();
        const cleanup = () => signal?.removeEventListener("abort", abortRequest);
        request.open("POST", safeApiPath("/api/v1/assets"));
        request.withCredentials = true;
        request.setRequestHeader("Accept", "application/json");
        const csrf = csrfTokenForRequest();
        if (csrf) request.setRequestHeader("X-CSRF-Token", csrf);
        request.upload.addEventListener("progress", (event) => {
            if (!event.lengthComputable || event.total <= 0) return;
            onProgress(Math.max(0, Math.min(99, Math.round(event.loaded / event.total * 100))));
        });
        request.addEventListener("load", () => {
            if (request.status < 200 || request.status >= 300) {
                cleanup();
                reject(new Error("媒体上传失败，请重试。"));
                return;
            }
            try {
                const asset = kind === "library"
                    ? libraryAssetFromResponse(JSON.parse(request.responseText) as unknown, mediaType)
                    : ownedAssetFromResponse(JSON.parse(request.responseText) as unknown, mediaType);
                onProgress(100);
                cleanup();
                resolve(asset);
            } catch {
                cleanup();
                reject(new Error("媒体上传响应无效，请重试。"));
            }
        });
        request.addEventListener("error", () => { cleanup(); reject(new Error("媒体上传失败，请检查网络后重试。")); });
        request.addEventListener("abort", () => { cleanup(); reject(new DOMException("The upload was cancelled.", "AbortError")); });
        const body = new FormData();
        body.append("kind", kind);
        body.append("media_type", mediaType);
        body.append("file", file, file.name);
        signal?.addEventListener("abort", abortRequest, { once: true });
        request.send(body);
    });
}

export function uploadMediaAsset(file: File, mediaType: GraphMediaType, onProgress: (percent: number) => void = () => undefined, signal?: AbortSignal): Promise<OwnedMediaAsset> {
    return uploadAsset(file, mediaType, "reference", onProgress, signal) as Promise<OwnedMediaAsset>;
}

function libraryAssetFromResponse(value: unknown, expectedMediaType: GraphMediaType): AssetRef {
    const asset = assetFromResponse(value);
    if (asset.kind !== "library" || asset.media_type !== expectedMediaType || typeof asset.content_url !== "string") {
        throw new Error("资产库响应无效，请重试。");
    }
    return asset;
}

export function uploadLibraryAsset(file: File, onProgress: (percent: number) => void = () => undefined, signal?: AbortSignal): Promise<AssetRef> {
    return uploadAsset(file, "image", "library", onProgress, signal);
}

export async function fetchLibraryAssets(): Promise<AssetRef[]> {
    const response = await apiFetch<{ assets: unknown[] }>("/api/v1/library-assets");
    if (!response || !Array.isArray(response.assets)) throw new Error("资产库响应无效，请重试。");
    return response.assets.map((item) => {
        const asset = assetFromResponse(item);
        if (asset.kind !== "library") throw new Error("资产库响应无效，请重试。");
        return asset;
    });
}
