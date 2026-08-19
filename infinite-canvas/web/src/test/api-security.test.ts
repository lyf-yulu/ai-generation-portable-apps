import { afterEach, expect, it, vi } from "vitest";
import { apiFetch, assetUrl } from "@/api/client";

afterEach(() => vi.unstubAllGlobals());

it("only accepts normalized same-origin API paths and sends same-origin credentials", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(apiFetch("/api/v1/jobs?state=queued")).resolves.toEqual({ ok: true });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "same-origin" });
});

it.each(["https://example.test/api/v1/jobs", "//example.test/api/v1/jobs", "/jobs", "/api/v1/../admin", "/api/v1/%2e%2e/admin", "/api/v1/%252e%252e/admin", "/api/v1/a%2Fb"]) ("rejects unsafe API path %s", async (path) => {
    await expect(apiFetch(path)).rejects.toThrow("API requests");
});

it("creates protected result addresses only from asset IDs", () => {
    expect(assetUrl("asset-1")).toBe("/api/v1/assets/asset-1");
    expect(() => assetUrl("../other")).toThrow("API requests");
});
