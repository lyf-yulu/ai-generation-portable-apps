import { apiFetch } from "./client";

export type ActivityAsset = {
    asset_id: string;
    kind: "reference" | "portrait";
    mime_type: string;
    status: "processing" | "active" | "failed";
    size_bytes: number;
    created_at: string;
    updated_at: string;
};

export type ActivityJob = {
    id: string;
    service_id: string;
    operation: string;
    status: string;
    error_code: string | null;
    created_at: string;
    updated_at: string;
};

export const fetchActivityAssets = async () => (await apiFetch<{ assets: ActivityAsset[] }>("/api/v1/activity/assets")).assets;
export const fetchActivityJobs = async () => (await apiFetch<{ jobs: ActivityJob[] }>("/api/v1/activity/jobs")).jobs;
