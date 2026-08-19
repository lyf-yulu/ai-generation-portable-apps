import type { StateStorage } from "zustand/middleware";
import { captureScopedStore, isStorageLeaseActive, type ScopedStoreLease } from "@/storage/scope";

export function captureAppStorageLease() {
    return captureScopedStore("app_state");
}

export async function setItemForLease(lease: ScopedStoreLease, name: string, value: string) {
    if (!isStorageLeaseActive(lease)) return false;
    await lease.store.setItem(name, value);
    return isStorageLeaseActive(lease);
}

export function createScopedPersistStorage(): StateStorage {
return {
    getItem: async (name) => {
        if (typeof window === "undefined") return null;
        const lease = captureAppStorageLease();
        if (!lease) return null;
        try {
            const value = (await lease.store.getItem<string>(name)) || null;
            return isStorageLeaseActive(lease) ? value : null;
        } catch {
            return null;
        }
    },
    setItem: async (name, value) => {
        if (typeof window === "undefined") return;
        const lease = captureAppStorageLease();
        if (!lease) return;
        try {
            await setItemForLease(lease, name, value);
        } catch { /* Do not fall back to an unscoped browser store. */ }
    },
    removeItem: async (name) => {
        if (typeof window === "undefined") return;
        const lease = captureAppStorageLease();
        if (!lease) return;
        try {
            if (!isStorageLeaseActive(lease)) return;
            await lease.store.removeItem(name);
        } catch { /* Do not fall back to an unscoped browser store. */ }
    },
};
}

export const localForageStorage: StateStorage = createScopedPersistStorage();
