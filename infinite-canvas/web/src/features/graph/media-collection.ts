import type { GraphMediaItem, GraphMediaType } from "@/features/graph/contracts";

const mediaLabels: Readonly<Record<GraphMediaType, string>> = {
    image: "图片",
    video: "视频",
    audio: "音频",
};

export function mediaItemLabel(mediaType: GraphMediaType, index: number) {
    return `@${mediaLabels[mediaType]}${index + 1}`;
}

export function moveMediaItem(items: readonly GraphMediaItem[], itemId: string, offset: -1 | 1): GraphMediaItem[] | readonly GraphMediaItem[] {
    const index = items.findIndex((item) => item.id === itemId);
    const target = index + offset;
    if (index < 0 || target < 0 || target >= items.length) return items;
    const next = [...items];
    const [item] = next.splice(index, 1);
    next.splice(target, 0, item);
    return next;
}

export function moveMediaItemTo(items: readonly GraphMediaItem[], itemId: string, targetId: string): GraphMediaItem[] | readonly GraphMediaItem[] {
    const index = items.findIndex((item) => item.id === itemId);
    const target = items.findIndex((item) => item.id === targetId);
    if (index < 0 || target < 0 || index === target) return items;
    const next = [...items];
    const [item] = next.splice(index, 1);
    next.splice(target, 0, item);
    return next;
}

export function safeMediaDisplayName(name: string, mediaType: GraphMediaType) {
    const basename = name.replace(/[\u0000-\u001f\u007f]/g, "").split(/[\\/]/).at(-1) ?? "";
    const cleaned = basename.trim().slice(0, 120);
    return cleaned || `${mediaLabels[mediaType]}文件`;
}
