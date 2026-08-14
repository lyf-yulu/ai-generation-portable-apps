import { apiFetch } from "./client";
import type { CanvasProjectInput } from "@/features/graph/normalize-project";
import type { CanvasProject } from "@/stores/canvas/use-canvas-store";


export type ProjectEnvelope = { project: CanvasProjectInput; version: number };
const jsonHeaders = { "Content-Type": "application/json" };

export const listProjects = async (signal?: AbortSignal) => (await apiFetch<{ projects: ProjectEnvelope[] }>("/api/v1/projects", { signal })).projects;
export const createProject = (project: CanvasProject, signal?: AbortSignal) => apiFetch<ProjectEnvelope>("/api/v1/projects", {
    method: "POST", headers: jsonHeaders, body: JSON.stringify(project), signal,
});
export const getProject = (id: string, signal?: AbortSignal) => apiFetch<ProjectEnvelope>(`/api/v1/projects/${encodeURIComponent(id)}`, { signal });
export const updateProject = (project: CanvasProject, expectedVersion: number, signal?: AbortSignal) => apiFetch<ProjectEnvelope>(`/api/v1/projects/${encodeURIComponent(project.id)}`, {
    method: "PUT", headers: jsonHeaders, body: JSON.stringify({ ...project, expected_version: expectedVersion }), signal,
});
export const deleteProject = (id: string, signal?: AbortSignal) => apiFetch<void>(`/api/v1/projects/${encodeURIComponent(id)}`, { method: "DELETE", signal });
