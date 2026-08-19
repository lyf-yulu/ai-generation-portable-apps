import { apiFetch } from "./client";
import type { SessionResponse } from "./contracts";
export const fetchSession = () => apiFetch<SessionResponse>("/api/v1/session");
