import { apiFetch } from "./client";
import type { ModelSpec } from "./contracts";
export const fetchModels = async () => (await apiFetch<{ models: ModelSpec[] }>("/api/v1/models")).models;
