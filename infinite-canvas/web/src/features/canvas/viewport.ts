import type { ViewportTransform } from "@/types/canvas";

export const MIN_CANVAS_SCALE = 0.05;
export const MAX_CANVAS_SCALE = 5;
export const RESET_VIEWPORT: ViewportTransform = Object.freeze({ x: 0, y: 0, k: 1 });

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const clampScale = (value: number) => Math.min(MAX_CANVAS_SCALE, Math.max(MIN_CANVAS_SCALE, value));

export function normalizeViewport(value: unknown): ViewportTransform {
    if (!value || typeof value !== "object") return { ...RESET_VIEWPORT };
    const candidate = value as Partial<ViewportTransform>;
    if (!finite(candidate.x) || !finite(candidate.y) || !finite(candidate.k) || candidate.k <= 0) return { ...RESET_VIEWPORT };
    return { x: candidate.x, y: candidate.y, k: clampScale(candidate.k) };
}

export function zoomViewportAt(viewport: ViewportTransform, pointer: { x: number; y: number }, deltaY: number): ViewportTransform {
    const current = normalizeViewport(viewport);
    const nextScale = clampScale(current.k * Math.pow(1.1, -deltaY / 100));
    const worldX = (pointer.x - current.x) / current.k;
    const worldY = (pointer.y - current.y) / current.k;
    return { x: pointer.x - worldX * nextScale, y: pointer.y - worldY * nextScale, k: nextScale };
}
