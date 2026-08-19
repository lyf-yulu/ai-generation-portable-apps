import { apiFetch, csrfTokenForRequest, safeApiPath } from "./client";
import type { ModelSpec, PortalSession } from "./contracts";


export type AdminUser = PortalSession & {
    display_name: string;
    enabled: boolean;
    must_change_password: boolean;
    approval_status: "pending" | "approved";
    model_ids: string[];
    comfy_workflow_ids: string[];
    created_at: number;
    updated_at: number;
};

export type AdminRegistration = {
    user_id: string;
    username: string;
    display_name: string;
    created_at: string;
};

export type AdminOperationContract = {
    operation: "image.generate" | "image.edit" | "video.generate";
    input_ports: Array<{ port_id: string; media_type: "text" | "image" | "video" | "audio"; min_items: number; max_items: number; asset_kind?: "library" }>;
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
    // Windows may report .json files as octet-stream; pin the JSON type explicitly.
    body.set("file", new Blob([file], { type: "application/json" }), file.name);
    return apiFetch<{ pools: AdminCredentialPool[] }>("/api/v1/admin/credential-pools/import", { method: "POST", body });
};

export type AdminAssetLibrary = {
    enabled: boolean;
    import_configured: boolean;
    has_ark_access: boolean;
    has_tos_access: boolean;
    tos_bucket?: string;
    tos_region?: string;
    project_name?: string;
    revision_digest?: string;
    default_group_id?: string;
};
export type AdminAssetLibraryGroup = { group_id: string; name: string };
export const fetchAdminAssetLibrary = () => apiFetch<AdminAssetLibrary>("/api/v1/admin/asset-library");
export const importAdminAssetLibrary = (file: File) => {
    const body = new FormData();
    // Windows may report .json files as octet-stream; pin the JSON type explicitly.
    body.set("file", new Blob([file], { type: "application/json" }), file.name);
    return apiFetch<AdminAssetLibrary>("/api/v1/admin/asset-library/import", { method: "POST", body });
};
export const fetchAdminAssetLibraryGroups = async () => (await apiFetch<{ groups: AdminAssetLibraryGroup[] }>("/api/v1/admin/asset-library/groups")).groups;

export type AdminArkKey = {
    configured: boolean;
    has_key: boolean;
};
export const fetchAdminArkKey = () => apiFetch<AdminArkKey>("/api/v1/admin/ark-key");
export const importAdminArkKey = (file: File) => {
    const body = new FormData();
    // Windows may report .json files as octet-stream; pin the JSON type explicitly.
    body.set("file", new Blob([file], { type: "application/json" }), file.name);
    return apiFetch<AdminArkKey>("/api/v1/admin/ark-key/import", { method: "POST", body });
};

export const downloadAdminConfigExample = (kind: "ark-key" | "credential-pools" | "asset-library" | "comfy-workflow") => {
    const path = safeApiPath(`/api/v1/admin/config-examples/${encodeURIComponent(kind)}`);
    const headers = new Headers({ Accept: "application/json" });
    const csrfToken = csrfTokenForRequest();
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    return fetch(path, { credentials: "same-origin", headers }).then(async (response) => {
        if (!response.ok) throw new Error("config example download failed");
        const filename = /^attachment;\s*filename="([A-Za-z0-9._-]+)"$/i.exec(response.headers.get("content-disposition") || "")?.[1] || `${kind}.example.json`;
        return { blob: await response.blob(), filename };
    });
};

export type AdminLogFile = { name: string; size: number; mtime: number };
export type AdminLogContent = { file: string; lines: number; window_total: number; truncated: boolean; log_lines: string[] };

export const fetchAdminLogFiles = async () => (await apiFetch<{ files: AdminLogFile[] }>("/api/v1/admin/logs/files")).files;

export const fetchAdminLogContent = (file: string, opts: { lines?: number; level?: string; q?: string } = {}) => {
    const params = new URLSearchParams({ file });
    if (opts.lines !== undefined) params.set("lines", String(opts.lines));
    if (opts.level) params.set("level", opts.level);
    if (opts.q) params.set("q", opts.q);
    return apiFetch<AdminLogContent>(`/api/v1/admin/logs/content?${params.toString()}`);
};

export const downloadAdminLog = (name: string) => {
    const path = safeApiPath(`/api/v1/admin/logs/download?file=${encodeURIComponent(name)}`);
    const headers = new Headers({ Accept: "text/plain" });
    const csrfToken = csrfTokenForRequest();
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    return fetch(path, { credentials: "same-origin", headers }).then(async (response) => {
        if (!response.ok) throw new Error("log download failed");
        const filename = /^attachment;\s*filename="([A-Za-z0-9._-]+)"$/i.exec(response.headers.get("content-disposition") || "")?.[1] || name;
        return { blob: await response.blob(), filename };
    });
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

export const setAdminUserPassword = (userId: string, newPassword: string, mustChangePassword: boolean) => apiFetch<AdminUser>(`/api/v1/admin/users/${encodeURIComponent(userId)}/password`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ new_password: newPassword, must_change_password: mustChangePassword }),
});

export const fetchAdminRegistrations = async () => (await apiFetch<{ registrations: AdminRegistration[] }>("/api/v1/admin/registrations")).registrations;
export const approveAdminRegistration = (userId: string) => apiFetch<AdminUser>(`/api/v1/admin/registrations/${encodeURIComponent(userId)}/approve`, { method: "POST", headers: jsonHeaders });
export const rejectAdminRegistration = (userId: string) => apiFetch<void>(`/api/v1/admin/registrations/${encodeURIComponent(userId)}/reject`, { method: "POST", headers: jsonHeaders });

export const replaceAdminUserModels = (userId: string, modelIds: string[]) => apiFetch<{ user_id: string; model_ids: string[] }>(`/api/v1/admin/users/${encodeURIComponent(userId)}/models`, {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify({ model_ids: modelIds }),
});
