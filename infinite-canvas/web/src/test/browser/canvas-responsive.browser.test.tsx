import { page } from "vitest/browser";
import { createRoot, type Root } from "react-dom/client";
import { flushSync } from "react-dom";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { AdminLogicalModel, AdminModelRoute, LogicalModelWrite, ModelRouteWrite } from "@/api/admin";
import { ProductShell } from "@/components/layout/product-shell";
import CanvasProjectPage from "@/pages/canvas/project";
import AdminModelsPage from "@/pages/admin/models";
import { clearCanvasInMemory, useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { useSessionStore } from "@/stores/portal/use-session-store";
import { clearStorageScope, setStorageScope } from "@/storage/scope";
import "@/styles/globals.css";

let root: Root;

const contract = {
    operation: "image.edit" as const,
    input_ports: [
        { port_id: "prompt", media_type: "text" as const, min_items: 1, max_items: 1 },
        { port_id: "reference_images", media_type: "image" as const, min_items: 1, max_items: 10 },
    ],
    output_media_type: "image" as const,
    parameter_schema: {
        type: "object",
        properties: {
            size: { type: "string", enum: ["auto", "1024x1024", "1024x1536", "1536x1024"], default: "auto" },
            output_count: { type: "integer", minimum: 1, maximum: 4, default: 1 },
        },
        required: ["size", "output_count"],
        additionalProperties: false,
    },
    parameter_mappings: { size: "size", output_count: "n" },
};

function json(body: unknown, status = 200) {
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function problem(code: string, references?: Record<string, number>) {
    return json({ code, message: "safe", retryable: false, request_id: "browser-fixture", phase: "request", references }, 409);
}

function installAdminApi({ startEmpty = false } = {}) {
    let model: AdminLogicalModel | null = startEmpty
        ? null
        : {
              model_id: "nano-banana",
              display_name: "Nano Banana",
              introduction: "Multi-reference edit",
              modality: "image",
              operation_contracts: [contract],
              enabled: true,
              archived_at: null,
              revision: 1,
          };
    let routes: AdminModelRoute[] = [];
    let assignment: string[] = [];
    let routeUpdateConflict = true;
    const calls: Array<{ url: string; method: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
        calls.push({ url, method, body });
        if (url.endsWith("/api/v1/admin/users"))
            return json({ users: [{ user_id: "ordinary-user", username: "canvas-user", display_name: "普通用户", role: "user", enabled: true, must_change_password: false, model_ids: assignment, created_at: 1, updated_at: 1 }] });
        if (url.endsWith("/api/v1/admin/models"))
            return json({
                models: [
                    {
                        model_id: "nano-banana",
                        service_id: "nano-banana",
                        display_name: "Nano Banana Offline",
                        operations: ["image.edit"],
                        input_media: ["text", "image"],
                        parameter_schema: contract.parameter_schema,
                        input_ports: contract.input_ports,
                        parameter_mappings: contract.parameter_mappings,
                    },
                ],
            });
        if (url.includes("/api/v1/admin/credential-pools"))
            return json({
                pools: [
                    {
                        pool_id: "banana-chiyun",
                        provider_id: "chiyun-banana",
                        adapter_type: "chiyun_gemini_images",
                        group: "banana",
                        allowed_families: ["nano-banana"],
                        revision_digest: "a".repeat(64),
                        key_count: 2,
                        total_capacity: 2,
                        capacity_status: "available",
                        available_count: 2,
                        busy_count: 0,
                        circuit_status: "unsupported",
                        circuit_open_count: null,
                    },
                    {
                        pool_id: "banana-t8-gemini",
                        provider_id: "t8star",
                        adapter_type: "chiyun_openai_images",
                        group: "gemini",
                        allowed_families: ["nano-banana"],
                        revision_digest: "b".repeat(64),
                        key_count: 1,
                        total_capacity: 1,
                        capacity_status: "available",
                        available_count: 1,
                        busy_count: 0,
                        circuit_status: "unsupported",
                        circuit_open_count: null,
                    },
                    {
                        pool_id: "t8-cc",
                        provider_id: "t8star",
                        adapter_type: "chiyun_openai_images",
                        group: "cc",
                        allowed_families: ["claude"],
                        revision_digest: "c".repeat(64),
                        key_count: 1,
                        total_capacity: 1,
                        capacity_status: "available",
                        available_count: 1,
                        busy_count: 0,
                        circuit_status: "unsupported",
                        circuit_open_count: null,
                    },
                ],
            });
        if (url.includes(`/api/v1/admin/users/ordinary-user/models`) && method === "PUT") {
            assignment = [...(body as { model_ids: string[] }).model_ids];
            return json({ user_id: "ordinary-user", model_ids: assignment });
        }
        if (url.match(/\/api\/v1\/admin\/logical-models\/nano-banana\/routes\?/) && method === "GET") {
            const includeArchived = url.includes("include_archived=true");
            return json({ routes: includeArchived ? routes : routes.filter((item) => !item.archived_at) });
        }
        if (url.endsWith("/api/v1/admin/logical-models/nano-banana/routes") && method === "POST") {
            const write = body as ModelRouteWrite;
            const created: AdminModelRoute = { ...write, enabled: false, archived_at: null, revision: 1 };
            routes = [...routes, created];
            return json(created, 201);
        }
        const routeMatch = url.match(/\/api\/v1\/admin\/logical-models\/nano-banana\/routes\/([^/?]+)(?:\/(enable|disable|archive|restore))?$/);
        if (routeMatch && method === "PUT") {
            const routeId = decodeURIComponent(routeMatch[1]);
            const index = routes.findIndex((item) => item.route_id === routeId);
            if (routeUpdateConflict) {
                routeUpdateConflict = false;
                routes[index] = { ...routes[index], revision: routes[index].revision + 1 };
                return problem("REVISION_CONFLICT");
            }
            const write = body as ModelRouteWrite & { revision: number };
            routes[index] = { ...routes[index], ...write, revision: routes[index].revision + 1 };
            return json(routes[index]);
        }
        if (routeMatch && routeMatch[2] && method === "POST") {
            const routeId = decodeURIComponent(routeMatch[1]);
            const action = routeMatch[2];
            const index = routes.findIndex((item) => item.route_id === routeId);
            const current = routes[index];
            routes[index] = {
                ...current,
                enabled: action === "enable" ? true : false,
                archived_at: action === "archive" ? "2026-08-12T01:00:00Z" : action === "restore" ? null : current.archived_at,
                revision: current.revision + 1,
            };
            return json(routes[index]);
        }
        if (url.endsWith("/api/v1/admin/logical-models") && method === "POST") {
            const write = body as LogicalModelWrite;
            model = { ...write, enabled: false, archived_at: null, revision: 1 };
            return json(model, 201);
        }
        if (url.endsWith("/api/v1/admin/logical-models/nano-banana") && method === "PUT") {
            const write = body as LogicalModelWrite & { revision: number };
            model = { ...model!, ...write, revision: model!.revision + 1 };
            return json(model);
        }
        if (url.endsWith("/api/v1/admin/logical-models/nano-banana/archive") && method === "POST") {
            model = { ...model!, enabled: false, archived_at: "2026-08-12T00:00:00Z", revision: model!.revision + 1 };
            return json(model);
        }
        if (url.endsWith("/api/v1/admin/logical-models/nano-banana/restore") && method === "POST") {
            model = { ...model!, enabled: false, archived_at: null, revision: model!.revision + 1 };
            return json(model);
        }
        if (url.match(/\/api\/v1\/admin\/logical-models\/nano-banana\?revision=/) && method === "DELETE") return problem("RESOURCE_REFERENCED", { route: 2, assignment: 1 });
        if (routeMatch && method === "GET") {
            return json(routes.find((item) => url.endsWith(`/${item.route_id}`)));
        }
        if (url.endsWith("/api/v1/admin/logical-models/nano-banana")) return json(model);
        if (url.includes("/api/v1/admin/logical-models?")) {
            const includeArchived = url.includes("include_archived=true");
            return json({ models: model && (!model.archived_at || includeArchived) ? [model] : [] });
        }
        if (url.endsWith("/api/v1/models"))
            return json({
                models:
                    model && assignment.includes(model.model_id)
                        ? [
                              {
                                  model_id: model.model_id,
                                  service_id: model.model_id,
                                  display_name: model.display_name,
                                  operations: ["image.edit"],
                                  input_media: ["text", "image"],
                                  parameter_schema: contract.parameter_schema,
                                  input_ports: contract.input_ports,
                                  parameter_mappings: contract.parameter_mappings,
                              },
                          ]
                        : [],
            });
        return json({});
    });
    vi.stubGlobal("fetch", fetch);
    return { calls, model: () => model, routes: () => routes, assignment: () => assignment };
}

async function frame() {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
}

beforeEach(async () => {
    await setStorageScope({ environment: "test", userId: "admin" });
    clearCanvasInMemory();
    useCanvasStore.setState({ hydrated: true, projectsLoaded: true });
    useSessionStore.setState({ session: { user_id: "admin", username: "canvas-admin", role: "admin", must_change_password: false }, environment: "test", loading: false, errorCode: null });
    document.body.innerHTML = '<div id="task8-browser-root"></div>';
    root = createRoot(document.getElementById("task8-browser-root")!);
});

afterEach(() => {
    flushSync(() => root.unmount());
    vi.unstubAllGlobals();
    clearCanvasInMemory();
    clearStorageScope();
    document.body.replaceChildren();
});

it("runs the desktop administrator route, lifecycle, assignment and canvas-node workflow", async () => {
    await page.viewport(1280, 900);
    const state = installAdminApi({ startEmpty: true });
    flushSync(() =>
        root.render(
            <MemoryRouter>
                <ProductShell>
                    <AdminModelsPage />
                </ProductShell>
            </MemoryRouter>,
        ),
    );
    await expect.element(page.getByRole("heading", { name: "模型与调用线路" })).toBeVisible();

    await page.getByRole("button", { name: "新建" }).click();
    await page.getByLabelText("模型 ID").fill("nano-banana");
    await page.getByLabelText("模型显示名").fill("Nano Banana");
    await page.getByLabelText("模型介绍").fill("Offline multi-reference image model");
    await page.getByLabelText("能力模板").selectOptions("multi_image");
    await page.getByLabelText("模型类型").selectOptions("banana");
    await page.getByRole("button", { name: "保存模型" }).click();
    await expect.element(page.getByText("Nano Banana", { exact: true }).first()).toBeVisible();
    const createCall = state.calls.find((item) => item.method === "POST" && item.url.endsWith("/logical-models"));
    expect(createCall?.body).toMatchObject({ model_id: "nano-banana", enabled: false });
    expect(JSON.stringify(createCall)).not.toMatch(/secret|api[_ -]?key|base[_ ]?url/i);

    await page.getByLabelText("模型显示名").fill("Nano Banana Offline");
    await page.getByRole("button", { name: "保存模型" }).click();
    await expect.element(page.getByLabelText("模型显示名")).toHaveValue("Nano Banana Offline");
    expect(state.calls.find((item) => item.method === "PUT" && item.url.endsWith("/logical-models/nano-banana"))?.body).toMatchObject({ revision: 1, display_name: "Nano Banana Offline" });

    const addRoute = async (provider: "Chiyun", pool: string) => {
        await page.getByLabelText(`${provider} 凭据池`).selectOptions(pool);
        await page.getByRole("button", { name: `保存 ${provider} 设置` }).click();
        await expect.element(page.getByLabelText(`启用 ${provider}`)).toBeVisible();
    };
    await addRoute("Chiyun", "banana-chiyun");
    expect(state.routes().map((route) => route.credential_pool_ref)).toEqual(["banana-chiyun"]);
    expect(document.body.textContent).toContain("可用 2");
    expect(document.body.textContent).not.toMatch(/offline-fixture-secret|api key|base url/i);
    for (const label of ["线路 ID", "线路模板", "Provider", "模型族", "供应商模型名"]) await expect.element(page.getByText(label, { exact: true })).not.toBeInTheDocument();

    await page.getByLabelText("Chiyun 优先级").fill("9");
    await page.getByLabelText("Chiyun 最大并发").fill("3");
    await page.getByRole("button", { name: "保存 Chiyun 设置" }).click();
    await expect.element(page.getByRole("alert")).toHaveTextContent("配置已变化，请重新加载");
    await page.getByRole("button", { name: "重新加载" }).click();
    await expect.element(page.getByLabelText("Chiyun 优先级")).toHaveValue(100);
    await page.getByLabelText("Chiyun 优先级").fill("9");
    await page.getByLabelText("Chiyun 最大并发").fill("3");
    await page.getByRole("button", { name: "保存 Chiyun 设置" }).click();
    await expect.element(page.getByLabelText("Chiyun 优先级")).toHaveValue(9);
    const routeUpdates = state.calls.filter((item) => item.method === "PUT" && item.url.endsWith("/routes/nano-banana-chiyun"));
    expect(routeUpdates).toHaveLength(2);
    expect(routeUpdates[0].body).toMatchObject({ revision: 1, provider_id: "chiyun-banana", provider_model_name: "banana2-ssvip", priority: 9, max_concurrency: 3 });
    expect(routeUpdates[1].body).toMatchObject({ revision: 2, provider_id: "chiyun-banana", provider_model_name: "banana2-ssvip", priority: 9, max_concurrency: 3 });

    await page.getByLabelText("启用 Chiyun").click();
    await expect.element(page.getByLabelText("启用 Chiyun")).toBeChecked();
    await page.getByLabelText("启用 Chiyun").click();
    await expect.element(page.getByLabelText("启用 Chiyun")).not.toBeChecked();

    await page.getByLabelText("选择账号").selectOptions("ordinary-user");
    await page.getByLabelText("Nano Banana Offline").click();
    await page.getByRole("button", { name: "保存派发" }).click();
    await expect.element(page.getByText("派发已保存")).toBeVisible();
    expect(state.assignment()).toEqual(["nano-banana"]);

    await page.getByRole("button", { name: "归档" }).first().click();
    await expect.element(page.getByRole("list", { name: "逻辑模型列表" })).not.toHaveTextContent("Nano Banana Offline");
    await page.getByRole("checkbox", { name: "显示已归档" }).click();
    await expect.element(page.getByText("Nano Banana Offline", { exact: true }).first()).toBeVisible();
    await page.getByText("Nano Banana Offline", { exact: true }).first().click();
    await page.getByRole("button", { name: "恢复" }).first().click();
    await expect.element(page.getByRole("button", { name: "启用" }).first()).toBeVisible();

    await page.getByRole("button", { name: "删除" }).first().click();
    await page.getByLabelText("输入 Nano Banana Offline 确认删除").fill("Nano Banana Offline");
    await page.getByRole("button", { name: "确认删除" }).click();
    await expect.element(page.getByRole("alert")).toHaveTextContent("线路 2");
    await expect.element(page.getByRole("alert")).toHaveTextContent("账号派发 1");
    (await page.getByLabelText("输入 Nano Banana Offline 确认删除").element()).dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape" }));

    flushSync(() => root.unmount());
    document.body.innerHTML = '<div id="task8-canvas-root"></div>';
    root = createRoot(document.getElementById("task8-canvas-root")!);
    useSessionStore.setState({ session: { user_id: "ordinary-user", username: "canvas-user", role: "user", must_change_password: false } });
    await setStorageScope({ environment: "test", userId: "ordinary-user" });
    const projectId = useCanvasStore.getState().createProject("Task8 offline canvas");
    flushSync(() =>
        root.render(
            <MemoryRouter initialEntries={[`/canvas/${projectId}`]}>
                <Routes>
                    <Route path="/canvas/:id" element={<CanvasProjectPage />} />
                </Routes>
            </MemoryRouter>,
        ),
    );
    await expect.element(page.getByRole("button", { name: "图片生成" })).not.toBeDisabled();
    await page.getByRole("button", { name: "图片生成" }).click();
    expect(
        useCanvasStore
            .getState()
            .openProject(projectId)!
            .nodes.some((node) => node.metadata?.graph?.role === "model" && node.metadata.graph.modelId === "nano-banana"),
    ).toBe(true);
});

it.each([415, 240])("keeps every administrator action reachable without page overflow at %i px", async (width) => {
    await page.viewport(width, 1100);
    installAdminApi();
    flushSync(() =>
        root.render(
            <MemoryRouter>
                <ProductShell>
                    <AdminModelsPage />
                </ProductShell>
            </MemoryRouter>,
        ),
    );
    await expect.element(page.getByRole("heading", { name: "模型与调用线路" })).toBeVisible();
    await frame();
    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(window.innerWidth);
    for (const name of ["保存模型", "保存 Chiyun 设置", "保存派发", "归档", "删除"]) {
        const button = page.getByRole("button", { name }).first();
        await expect.element(button).toBeVisible();
        const rect = (await button.element()).getBoundingClientRect();
        expect(rect.left).toBeGreaterThanOrEqual(0);
        expect(rect.right).toBeLessThanOrEqual(window.innerWidth);
    }
    await frame();
    await expect.element(page.getByRole("button", { name: "保存 Chiyun 设置" })).toBeVisible();
    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(window.innerWidth);
    const saveRect = (await page.getByRole("button", { name: "保存 Chiyun 设置" }).element()).getBoundingClientRect();
    expect(saveRect.left).toBeGreaterThanOrEqual(0);
    expect(saveRect.right).toBeLessThanOrEqual(window.innerWidth);
});
