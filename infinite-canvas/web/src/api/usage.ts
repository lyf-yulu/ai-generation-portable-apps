import { apiFetch } from "./client";

export type UsageSummary = {
    successful_jobs: number;
    image_count: number;
    video_seconds: number;
    total_cost_fen: string;
};

export type ChargedUsageJob = {
    operation: string;
    status: string;
    model_id: string | null;
    route_id: string | null;
    video_seconds: number;
    image_count: number;
    video_price_fen: string;
    image_price_fen: string;
    cost_fen: string;
    charged_at: string;
};

export type Usage = {
    summary: UsageSummary;
    jobs: ChargedUsageJob[];
};

export type AdminUsage = Omit<Usage, "jobs"> & {
    users: Array<{ user_id: string; summary: UsageSummary }>;
    jobs: Array<ChargedUsageJob & { user_id: string }>;
};

export type UsageRates = {
    video_price_fen: number;
    image_price_fen: number;
};

const jsonHeaders = { "Content-Type": "application/json" };

export const fetchUsage = () => apiFetch<Usage>("/api/v1/usage");
export const fetchAdminUsage = () => apiFetch<AdminUsage>("/api/v1/admin/usage");
export const fetchUsageRates = () => apiFetch<UsageRates>("/api/v1/admin/usage/rates");
export const updateUsageRates = (videoPriceFen: number, imagePriceFen: number) =>
    apiFetch<UsageRates>("/api/v1/admin/usage/rates", {
        method: "PUT",
        headers: jsonHeaders,
        body: JSON.stringify({ video_price_fen: videoPriceFen, image_price_fen: imagePriceFen }),
    });
