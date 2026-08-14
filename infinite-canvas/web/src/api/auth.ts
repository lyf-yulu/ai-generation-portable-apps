import { apiFetch } from "./client";
import type { AuthResponse } from "./contracts";

const json = { "Content-Type": "application/json" };

export const loginLocal = (username: string, password: string) =>
    apiFetch<AuthResponse>("/api/v1/auth/login", {
        method: "POST",
        headers: json,
        body: JSON.stringify({ username, password }),
    });

export const logoutLocal = () => apiFetch<void>("/api/v1/auth/logout", { method: "POST" });

export const changeLocalPassword = (currentPassword: string, newPassword: string) =>
    apiFetch<AuthResponse>("/api/v1/auth/change-password", {
        method: "POST",
        headers: json,
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });
