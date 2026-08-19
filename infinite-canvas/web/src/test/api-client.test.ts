import { afterEach, expect, it, vi } from "vitest";

import { ApiRequestError, apiFetch, setCsrfToken } from "@/api/client";
import { createProject as createProjectApi, updateProject as updateProjectApi } from "@/api/projects";
import type { CanvasProject } from "@/stores/canvas/use-canvas-store";

afterEach(() => {
    setCsrfToken(null);
    vi.unstubAllGlobals();
});

it("adds the in-memory csrf token only to same-origin mutations", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("csrf-memory-only");

    await apiFetch("/api/v1/projects", { method: "POST", body: "{}" });
    await apiFetch("/api/v1/projects");

    const mutationHeaders = new Headers(fetchMock.mock.calls[0][1].headers);
    const queryHeaders = new Headers(fetchMock.mock.calls[1][1].headers);
    expect(mutationHeaders.get("X-CSRF-Token")).toBe("csrf-memory-only");
    expect(queryHeaders.has("X-CSRF-Token")).toBe(false);
    expect(localStorage.getItem("csrf_token")).toBeNull();
});

it("sends the canonical graph schema version in real project create and update request shapes", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ project: {}, version: 1 }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const project: CanvasProject = {
        id: "shape-contract",
        title: "Shape contract",
        createdAt: "2026-08-11T00:00:00.000Z",
        updatedAt: "2026-08-11T00:00:00.000Z",
        nodes: [],
        connections: [],
        chatSessions: [],
        activeChatId: null,
        backgroundMode: "lines",
        showImageInfo: false,
        viewport: { x: 0, y: 0, k: 1 },
        graphSchemaVersion: 1,
    };

    await createProjectApi(project);
    await updateProjectApi(project, 3);

    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toMatchObject({ id: "shape-contract", graphSchemaVersion: 1 });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body as string)).toMatchObject({ id: "shape-contract", graphSchemaVersion: 1, expected_version: 3 });
});

it.each([
    [401, "unauthorized", false],
    [403, "forbidden", false],
    [429, "rate_limited", true],
    [500, "internal_error", true],
] as const)("normalizes HTTP %i to a stable ApiError", async (status, code, retryable) => {
    vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
            new Response(JSON.stringify({ code, message: "safe failure", request_id: "req-123", phase: "submit", retryable }), {
                status,
                headers: { "Content-Type": "application/json" },
            }),
        ),
    );

    await expect(apiFetch("/api/v1/jobs")).rejects.toMatchObject({ code, message: status >= 500 ? "The service failed to process the request." : "safe failure", request_id: "req-123", phase: "submit", retryable } satisfies Partial<ApiRequestError>);
});

it("normalizes non-JSON failures without exposing response content", async () => {
    const sensitiveResponse = `${["api", "key"].join("_")}=private\nTraceback: internal stack`;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(sensitiveResponse, { status: 500, headers: { "Content-Type": "text/plain", "X-Request-Id": "req-header" } })));

    await expect(apiFetch("/api/v1/jobs")).rejects.toMatchObject({
        code: "internal_error",
        message: "The service failed to process the request.",
        request_id: "req-header",
        phase: "response",
        retryable: true,
    } satisfies Partial<ApiRequestError>);
});

it("uses the fixed local message for 5xx JSON responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: "internal_error", message: "Traceback /srv/private.py: secret" }), { status: 500, headers: { "Content-Type": "application/json" } })));
    await expect(apiFetch("/api/v1/jobs")).rejects.toMatchObject({ message: "The service failed to process the request." });
});

it("does not expose filesystem and exception details in 4xx messages", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ message: "ENOENT /srv/private.py" }), { status: 403, headers: { "Content-Type": "application/json" } })));
    await expect(apiFetch("/api/v1/jobs")).rejects.toMatchObject({ message: "You are not allowed to perform this action." });
});

it.each(["OSError ('/srv/private.py')", "failed (C:/private.txt)", "/private.py", "failed:/srv/private.py", "path=/private.py", "file:///srv/private", "at foo (/srv/x.ts:12:3)", "bad\u0007message", "Visit https://example.com/help"])("rejects unsafe 4xx detail %s", async (message) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ message }), { status: 403, headers: { "Content-Type": "application/json" } })));
    await expect(apiFetch("/api/v1/jobs")).rejects.toMatchObject({ message: "You are not allowed to perform this action." });
});

it("keeps a short single-line user-facing 4xx message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ message: "This item is not available." }), { status: 403, headers: { "Content-Type": "application/json" } })));
    await expect(apiFetch("/api/v1/jobs")).rejects.toMatchObject({ message: "This item is not available." });
});

it.each(["path=%2Fprivate.py", "path=%5Cprivate.py", "path=%252Fprivate.py", "path=%255Cprivate.py", "path=%25252Fprivate.py", "bad%ZZ"])("rejects encoded or malformed unsafe detail %s", async (message) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ message }), { status: 403, headers: { "Content-Type": "application/json" } })));
    await expect(apiFetch("/api/v1/jobs")).rejects.toMatchObject({ message: "You are not allowed to perform this action." });
});

it.each(["mailto:ops@example.com", "https:evil.example", "ssh://host/path", "git://host/repo", "blob:https://example", "vscode://x", "urn:isbn:123", "Details: [mailto:ops@example.com]", "note,urn:isbn:123", "x=SSH://host"])("rejects explicit URI scheme %s", async (message) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ message }), { status: 403, headers: { "Content-Type": "application/json" } })));
    await expect(apiFetch("/api/v1/jobs")).rejects.toMatchObject({ message: "You are not allowed to perform this action." });
});

it("keeps a normal colon in a short user-facing message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ message: "Please retry: the item is locked." }), { status: 403, headers: { "Content-Type": "application/json" } })));
    await expect(apiFetch("/api/v1/jobs")).rejects.toMatchObject({ message: "Please retry: the item is locked." });
});

it("retains only bounded reference categories from a conflict", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: "RESOURCE_REFERENCED", message: "safe", references: { job: 2, route: 1 } }), { status: 409, headers: { "Content-Type": "application/json" } })));
    await expect(apiFetch("/api/v1/admin/logical-models/model")).rejects.toMatchObject({ references: { job: 2, route: 1 } });
});

it.each([{ job: 2, secret: 1 }, { job: -1 }, { job: 1.5 }, { job: 1_000_001 }])("drops an invalid reference map %j", async (references) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: "RESOURCE_REFERENCED", message: "safe", references }), { status: 409, headers: { "Content-Type": "application/json" } })));
    try { await apiFetch("/api/v1/admin/logical-models/model"); } catch (error) { expect((error as ApiRequestError).references).toBeUndefined(); }
});
