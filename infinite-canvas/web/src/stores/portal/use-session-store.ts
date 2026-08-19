import { create } from "zustand";

import { changeLocalPassword, loginLocal, logoutLocal } from "@/api/auth";
import { ApiRequestError, setCsrfToken } from "@/api/client";
import { fetchSession } from "@/api/session";
import type { PortalSession } from "@/api/contracts";
import { clearStorageScope, setStorageScope } from "@/storage/scope";
import { captureAppStorageLease } from "@/lib/localforage-storage";
import { useAssetStore } from "@/stores/use-asset-store";
import { clearCanvasInMemory } from "@/stores/canvas/use-canvas-store";
import { clearGenerationPreferences } from "@/stores/use-config-store";

type PortalSessionStore = {
    session: PortalSession | null;
    environment: string | null;
    loading: boolean;
    errorCode: string | null;
    loadSession: (environment: string) => Promise<PortalSession>;
    setSession: (session: PortalSession, environment: string) => Promise<void>;
    login: (username: string, password: string, environment: string) => Promise<PortalSession>;
    logout: () => Promise<void>;
    changePassword: (currentPassword: string, newPassword: string) => Promise<PortalSession>;
    clearSession: () => void;
};

let sessionVersion = 0;

function clearInMemoryUserState() {
    clearCanvasInMemory();
    useAssetStore.setState({ assets: [], hydrated: false });
    clearGenerationPreferences();
}

export const useSessionStore = create<PortalSessionStore>()((set, get) => ({
    session: null,
    environment: null,
    loading: false,
    errorCode: null,
    loadSession: async (environment) => {
        const version = ++sessionVersion;
        set({ loading: true });
        try {
            const response = await fetchSession();
            const { csrf_token, ...session } = response;
            setCsrfToken(csrf_token || null);
            await activateSession(version, session, environment, set);
            return session;
        } catch (error) {
            if (version === sessionVersion) set({ errorCode: error instanceof ApiRequestError ? error.code : "SESSION_UNAVAILABLE" });
            throw error;
        } finally {
            if (version === sessionVersion) set({ loading: false });
        }
    },
    setSession: (session, environment) => activateSession(++sessionVersion, session, environment, set),
    login: async (username, password, environment) => {
        const version = ++sessionVersion;
        set({ loading: true, errorCode: null });
        try {
            const response = await loginLocal(username, password);
            setCsrfToken(response.csrf_token);
            await activateSession(version, response.user, environment, set);
            return response.user;
        } catch (error) {
            if (version === sessionVersion) set({ errorCode: error instanceof ApiRequestError ? error.code : "LOGIN_FAILED" });
            throw error;
        } finally {
            if (version === sessionVersion) set({ loading: false });
        }
    },
    logout: async () => {
        try {
            await logoutLocal();
        } finally {
            get().clearSession();
        }
    },
    changePassword: async (currentPassword, newPassword) => {
        const environment = get().environment || "local";
        const version = ++sessionVersion;
        set({ loading: true, errorCode: null });
        try {
            const response = await changeLocalPassword(currentPassword, newPassword);
            setCsrfToken(response.csrf_token);
            await activateSession(version, response.user, environment, set);
            return response.user;
        } catch (error) {
            if (version === sessionVersion) set({ errorCode: error instanceof ApiRequestError ? error.code : "PASSWORD_CHANGE_FAILED" });
            throw error;
        } finally {
            if (version === sessionVersion) set({ loading: false });
        }
    },
    clearSession: () => {
        sessionVersion += 1;
        setCsrfToken(null);
        clearStorageScope();
        clearInMemoryUserState();
        set({ session: null, environment: null, loading: false, errorCode: null });
    },
}));

async function activateSession(version: number, session: PortalSession, environment: string, set: (state: Partial<PortalSessionStore>) => void) {
    if (version !== sessionVersion) return;
    clearStorageScope();
    clearInMemoryUserState();
    set({ session: null, environment: null, errorCode: null });
    await setStorageScope({ environment, userId: session.user_id });
    if (version !== sessionVersion) return;
    const { useCanvasStore } = await import("@/stores/canvas/use-canvas-store");
    await Promise.all([useCanvasStore.persist.rehydrate(), useAssetStore.persist.rehydrate()]);
    const projectLease = captureAppStorageLease();
    if (projectLease) {
        const { projectSync } = await import("@/features/projects/project-sync");
        await projectSync.activate(projectLease);
    }
    if (version === sessionVersion) set({ session, environment, errorCode: null });
}
