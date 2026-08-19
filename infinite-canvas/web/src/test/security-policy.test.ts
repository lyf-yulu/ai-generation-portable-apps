import { expect, it } from "vitest";

import { SECURITY_POLICY } from "@/constant/security-policy";

it("keeps executable extensions and keys server-side", () => {
    expect(SECURITY_POLICY).toEqual({
        browserApiKeys: false,
        remotePlugins: false,
        dynamicModelScripts: false,
        arbitraryBackendUrls: false,
    });
});
