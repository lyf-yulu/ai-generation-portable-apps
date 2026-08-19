import { expect, it } from "vitest";
import { assetIdsForReferences } from "@/api/jobs";

it("requires references to be uploaded assets instead of silently dropping them", () => {
    expect(assetIdsForReferences([{ id: "local", asset_id: "asset-1" }])).toEqual(["asset-1"]);
    expect(() => assetIdsForReferences([{ id: "local", dataUrl: "data:image/png;base64,AA==" }])).toThrow("先上传资产");
});
