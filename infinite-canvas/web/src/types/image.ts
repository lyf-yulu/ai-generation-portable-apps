export type ReferenceImage = {
    id: string;
    name: string;
    type: string;
    dataUrl: string;
    url?: string;
    storageKey?: string;
    /** Assigned only after a same-origin asset upload; local IDs are not task assets. */
    asset_id?: string;
};
