import localforage from "localforage";

export type StorageScope = { environment: string; userId: string };

let activeScope: StorageScope | null = null;
let scopeVersion = 0;
const instances = new Map<string, LocalForage>();
const clearListeners = new Set<() => void>();
const scopeListeners = new Set<() => void>();
let instanceFactory: (options: { name: string; storeName: string }) => LocalForage = (options) => localforage.createInstance(options);

export class StorageScopeChangedError extends Error {
    constructor() {
        super("Browser storage scope changed while the operation was pending");
        this.name = "StorageScopeChangedError";
    }
}

export type ScopedStoreLease = { store: LocalForage; version: number };

function encodeScopeSegment(value: string) {
    return encodeURIComponent(value);
}

export function storageDatabaseName(scope: StorageScope) {
    return `ai-creation-canvas:${encodeScopeSegment(scope.environment)}:${encodeScopeSegment(scope.userId)}`;
}

export function currentStorageScope() {
    return activeScope;
}

export function currentStorageScopeVersion() {
    return scopeVersion;
}

export function isCurrentStorageScopeVersion(version: number) {
    return activeScope !== null && scopeVersion === version;
}

export async function setStorageScope(scope: StorageScope) {
    if (!scope.environment || !scope.userId) throw new Error("A Portal session and environment are required before opening browser storage");
    clearStorageScope();
    activeScope = { environment: scope.environment, userId: scope.userId };
    scopeListeners.forEach((listener) => listener());
}

export function clearStorageScope() {
    scopeVersion += 1;
    activeScope = null;
    instances.clear();
    clearListeners.forEach((listener) => listener());
}

export function scopedStore(storeName: string): LocalForage | null {
    if (!activeScope) return null;
    const key = `${storageDatabaseName(activeScope)}:${storeName}`;
    let instance = instances.get(key);
    if (!instance) {
        instance = instanceFactory({ name: storageDatabaseName(activeScope), storeName });
        instances.set(key, instance);
    }
    return instance;
}

/** Test-only dependency injection; production never calls this. */
export function setScopedStoreFactoryForTest(factory?: (options: { name: string; storeName: string }) => LocalForage) {
    instances.clear();
    instanceFactory = factory || ((options) => localforage.createInstance(options));
}

export function captureScopedStore(storeName: string): ScopedStoreLease | null {
    const store = scopedStore(storeName);
    return store ? { store, version: scopeVersion } : null;
}

export function isStorageLeaseActive(lease: ScopedStoreLease) {
    return isCurrentStorageScopeVersion(lease.version);
}

export function requireActiveStorageLease(lease: ScopedStoreLease) {
    if (!isStorageLeaseActive(lease)) throw new StorageScopeChangedError();
    return lease.store;
}

export function onStorageScopeCleared(listener: () => void) {
    clearListeners.add(listener);
    return () => clearListeners.delete(listener);
}
export function onStorageScopeChanged(listener: () => void) { scopeListeners.add(listener); return () => scopeListeners.delete(listener); }
