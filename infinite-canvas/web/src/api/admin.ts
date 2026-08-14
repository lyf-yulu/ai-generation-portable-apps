import { apiFetch } from "./client";
import type { ModelSpec, PortalSession } from "./contracts";


export type AdminUser = PortalSession & {
    display_name: string;
    enabled: boolean;
    must_change_password: boolean;
    model_ids: string[];
    created_at: number;
    updated_at: number;
};

export type AdminOperationContract = {
    operation: "image.generate" | "image.edit" | "video.generate";
    input_ports: Array<{ port_id: string; media_type: "text" | "image" | "video" | "audio"; min_items: number; max_items: number }>;
    output_media_type: "image" | "video";
    parameter_schema: Record<string, unknown>;
    parameter_mappings: Record<string, string>;
};
export type AdminLogicalModel = {
    model_id: string; display_name: string; introduction?: string; modality: "image" | "video";
    operation_contracts?: AdminOperationContract[]; enabled: boolean; archived_at: string | null; revision: number;
};
export type AdminModelRoute = {
    route_id: string; model_id: string; provider_id?: string; provider_model_name?: string;
    adapter_type?: "ark" | "chiyun_gemini_images" | "chiyun_openai_images"; credential_pool_ref?: string; family?: string;
    operation_contracts?: AdminOperationContract[]; priority?: number; max_concurrency?: number;
    enabled: boolean; archived_at: string | null; revision: number;
};
export type AdminCredentialPool = {
    pool_id: string; provider_id: string; adapter_type?: "ark" | "chiyun_gemini_images" | "chiyun_openai_images"; group: string; allowed_families: string[];
    revision_digest: string; key_count: number; total_capacity: number;
    capacity_status: "available" | "unavailable"; available_count: number | null; busy_count: number | null;
    circuit_status: "unsupported"; circuit_open_count: number | null;
};
export type AdminUsageCounters = { jobs: number; succeeded: number; failed: number; active: number; image: number; video: number };
export type AdminUserUsage = AdminUsageCounters & { user_id: string; username: string; display_name: string };
export type LogicalModelWrite = {
    model_id: string; display_name: string; introduction: string; modality: "image" | "video";
    operation_contracts: AdminOperationContract[]; enabled: boolean; revision?: number;
};
export type ModelRouteWrite = {
    route_id: string; model_id: string; provider_id: string; provider_model_name: string;
    adapter_type: "ark" | "chiyun_gemini_images" | "chiyun_openai_images"; credential_pool_ref: string; family: string;
    operation_contracts: AdminOperationContract[]; priority: number; max_concurrency: number; enabled: boolean; revision?: number;
};

const jsonHeaders = { "Content-Type": "application/json" };

export const fetchAdminUsers = async () => (await apiFetch<{ users: AdminUser[] }>("/api/v1/admin/users")).users;
export const fetchAdminUsage = () => apiFetch<{ totals: AdminUsageCounters; users: AdminUserUsage[] }>("/api/v1/admin/usage");
export const fetchAdminModels = async () => (await apiFetch<{ models: ModelSpec[] }>("/api/v1/admin/models")).models;

export const fetchAdminLogicalModels = async (includeArchived = false) => (await apiFetch<{ models: AdminLogicalModel[] }>(`/api/v1/admin/logical-models?include_archived=${includeArchived ? "true" : "false"}`)).models;
export const fetchAdminLogicalModel = (modelId: string) => apiFetch<AdminLogicalModel>(`/api/v1/admin/logical-models/${encodeURIComponent(modelId)}`);
export const createAdminLogicalModel = (body: LogicalModelWrite) => apiFetch<AdminLogicalModel>("/api/v1/admin/logical-models", { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) });
export const updateAdminLogicalModel = (body: LogicalModelWrite & { revision: number }) => apiFetch<AdminLogicalModel>(`/api/v1/admin/logical-models/${encodeURIComponent(body.model_id)}`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(body) });

export const fetchAdminModelRoutes = async (modelId: string, includeArchived = false) => (await apiFetch<{ routes: AdminModelRoute[] }>(`/api/v1/admin/logical-models/${encodeURIComponent(modelId)}/routes?include_archived=${includeArchived ? "true" : "false"}`)).routes;
export const fetchAdminModelRoute = (modelId: string, routeId: string) => apiFetch<AdminModelRoute>(`/api/v1/admin/logical-models/${encodeURIComponent(modelId)}/routes/${encodeURIComponent(routeId)}`);
export const createAdminModelRoute = (body: ModelRouteWrite) => apiFetch<AdminModelRoute>(`/api/v1/admin/logical-models/${encodeURIComponent(body.model_id)}/routes`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) });
export const updateAdminModelRoute = (body: ModelRouteWrite & { revision: number }) => apiFetch<AdminModelRoute>(`/api/v1/admin/logical-models/${encodeURIComponent(body.model_id)}/routes/${encodeURIComponent(body.route_id)}`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(body) });
export const fetchAdminCredentialPools = async () => (await apiFetch<{ pools: AdminCredentialPool[] }>("/api/v1/admin/credential-pools")).pools;
export const importAdminCredentialPools = (file: File) => {
    const body = new FormData();
    body.set("file", file, file.name);
    return apiFetch<{ pools: AdminCredentialPool[] }>("/api/v1/admin/credential-pools/import", { method: "POST", body });
};

type LifecycleKind = "enable" | "disable" | "archive" | "restore" | "purge-runtime";
const lifecycle = <T>(path: string, revision: number) => apiFetch<T>(path, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ revision }) });
export const changeAdminLogicalModelLifecycle = (modelId: string, action: LifecycleKind, revision: number) => lifecycle<AdminLogicalModel>(`/api/v1/admin/logical-models/${encodeURIComponent(modelId)}/${action}`, revision);
export const changeAdminModelRouteLifecycle = (modelId: string, routeId: string, action: LifecycleKind, revision: number) => lifecycle<AdminModelRoute>(`/api/v1/admin/logical-models/${encodeURIComponent(modelId)}/routes/${encodeURIComponent(routeId)}/${action}`, revision);
export const deleteAdminLogicalModel = (modelId: string, revision: number) => apiFetch<void>(`/api/v1/admin/logical-models/${encodeURIComponent(modelId)}?revision=${encodeURIComponent(String(revision))}`, { method: "DELETE" });
export const deleteAdminModelRoute = (modelId: string, routeId: string, revision: number) => apiFetch<void>(`/api/v1/admin/logical-models/${encodeURIComponent(modelId)}/routes/${encodeURIComponent(routeId)}?revision=${encodeURIComponent(String(revision))}`, { method: "DELETE" });

export const setAdminUserEnabled = (userId: string, enabled: boolean) => apiFetch<AdminUser>(`/api/v1/admin/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    headers: jsonHeaders,
    body: JSON.stringify({ enabled }),
});

export const replaceAdminUserModels = (userId: string, modelIds: string[]) => apiFetch<{ user_id: string; model_ids: string[] }>(`/api/v1/admin/users/${encodeURIComponent(userId)}/models`, {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify({ model_ids: modelIds }),
});
