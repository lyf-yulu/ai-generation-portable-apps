import { apiFetch, csrfTokenForRequest, safeApiPath } from "./client";

export type WorkflowFormat = "editor" | "api";
export type PreviewGraph = {
    nodes: Array<{ id: string; type: string; title: string | null; position: [number, number] | null }>;
    edges: Array<{ source_id: string; target_id: string }>;
    has_editor_layout: boolean;
};
export type WorkflowDependency = { type: string; is_core: boolean };
export type WorkflowRevision = {
    workflow_id: string;
    revision: number;
    formats: WorkflowFormat[];
    preview: PreviewGraph;
    dependencies: { node_types: WorkflowDependency[] };
    checksum_prefix: string;
    execution_available: boolean;
    execution_unavailable_reason?: string;
};
export type AdminComfyWorkflow = {
    workflow_id: string;
    display_name: string;
    description: string;
    service_id: string;
    lifecycle: { enabled: boolean; archived: boolean };
    revision: number;
    lifecycle_revision: number;
    checksum_prefix: string;
    execution_available: boolean;
    current_revision?: WorkflowRevision;
};
export type WorkflowImportMetadata = { displayName: string; serviceId: string };
export type ComfyWorkflowServiceCapability = {
    service_id: string;
    status: "healthy" | "unavailable" | "misconfigured";
    node_types: string[];
};
export type ComfyWorkflowCapabilities = {
    assignments: { available: boolean; reason?: "PORTAL_USER_DIRECTORY_UNAVAILABLE" };
    services: ComfyWorkflowServiceCapability[];
};

const jsonHeaders = { "Content-Type": "application/json" };

export function importAdminComfyWorkflow(file: File, metadata: WorkflowImportMetadata) {
    const body = new FormData();
    body.set("file", file, file.name);
    body.set("display_name", metadata.displayName);
    body.set("service_id", metadata.serviceId);
    return apiFetch<AdminComfyWorkflow>("/api/v1/admin/comfy-workflows/import", { method: "POST", body });
}

export const fetchAdminComfyWorkflows = async () => (await apiFetch<{ workflows: AdminComfyWorkflow[] }>("/api/v1/admin/comfy-workflows")).workflows;
export const fetchAdminComfyWorkflowCapabilities = () => apiFetch<ComfyWorkflowCapabilities>("/api/v1/admin/comfy-workflows/capabilities");
export const fetchAdminComfyWorkflow = (workflowId: string) => apiFetch<AdminComfyWorkflow>(`/api/v1/admin/comfy-workflows/${encodeURIComponent(workflowId)}`);
export const fetchAdminComfyWorkflowPreview = (workflowId: string, revision: number) => apiFetch<WorkflowRevision>(`/api/v1/admin/comfy-workflows/${encodeURIComponent(workflowId)}/revisions/${revision}/preview`);

export function exportAdminComfyWorkflow(workflowId: string, revision: number, format: WorkflowFormat) {
    const path = safeApiPath(`/api/v1/admin/comfy-workflows/${encodeURIComponent(workflowId)}/revisions/${revision}/export?format=${encodeURIComponent(format)}`);
    const headers = new Headers({ Accept: "application/json" });
    const csrfToken = csrfTokenForRequest();
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    return fetch(path, { credentials: "same-origin", headers }).then(async (response) => {
        if (!response.ok) throw new Error("workflow export failed");
        return { blob: await response.blob(), filename: exportFilename(response.headers.get("content-disposition"), workflowId, revision, format) };
    });
}

const exportFilename = (contentDisposition: string | null, workflowId: string, revision: number, format: WorkflowFormat) => {
    const match = /^attachment;\s*filename="([A-Za-z0-9._-]+)"$/i.exec(contentDisposition || "");
    return match?.[1] || `comfy-workflow-${workflowId}-r${revision}-${format}.json`;
};

const changeLifecycle = (workflowId: string, action: "enable" | "disable" | "archive" | "restore", revision: number) =>
    apiFetch<AdminComfyWorkflow>(`/api/v1/admin/comfy-workflows/${encodeURIComponent(workflowId)}/${action}`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ revision }) });

export const changeAdminComfyWorkflowLifecycle = changeLifecycle;
export const replaceAdminUserComfyWorkflows = (userId: string, workflowIds: string[]) =>
    apiFetch<{ user_id: string; workflow_ids: string[] }>(`/api/v1/admin/users/${encodeURIComponent(userId)}/comfy-workflows`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ workflow_ids: workflowIds }) });
