import type { ApiError } from "./contracts";

const API_PREFIX = "/api/v1/";
// 挂载点。Portal 把画布挂在 /infinite-canvas/ 下并在代理时剥掉该前缀，
// 所以浏览器要请求 /infinite-canvas/api/v1/*，子应用收到的仍是 /api/v1/*。
// 构建期 BASE_URL = "/infinite-canvas/"；npm run dev 时为 "/"，
// 下面的推导退化成恒等变换，本地开发行为不变。
//
// 注意：BASE_URL 会被 Vite 在编译期替换成字面量，三元/endsWith 这类写法
// 会被常量折叠（实测折叠出过 "/infinite-canvas//" 双斜杠）。用正则归一化，
// 结果与是否折叠无关。
const BASE = `/${String(import.meta.env.BASE_URL).replace(/^\/+|\/+$/g, "")}/`.replace(/^\/\/+/, "/");
const MOUNTED_PREFIX = `${BASE}api/v1/`;
/** 结果/素材地址是否指向受保护的 asset 接口（挂载态或裸态都认）。 */
export const isMountedAssetUrl = (url: string) =>
    url.startsWith(`${MOUNTED_PREFIX}assets/`) || url.startsWith(`${API_PREFIX}assets/`);
let csrfToken: string | null = null;

export function setCsrfToken(value: string | null) {
    csrfToken = value;
}

export function csrfTokenForRequest() {
    return csrfToken;
}
const unsafePathError = () => new Error("API requests must use a normalized relative /api/v1/ path");

function fullyDecode(value: string) {
    let decoded = value;
    for (let attempt = 0; attempt < 4; attempt += 1) {
        let next: string;
        try { next = decodeURIComponent(decoded); } catch { throw unsafePathError(); }
        if (next === decoded) return decoded;
        decoded = next;
    }
    return decoded;
}

/** Validates before URL normalization so encoded dot-segments cannot escape the API prefix.
 *  校验始终针对裸 /api/v1/ 路径，返回值带上挂载前缀；对已挂载的输入幂等，
 *  因为 assetUrl() 的返回值既当 <img src> 用也会再被 apiFetch 消费。 */
export function safeApiPath(path: string) {
    if (!path.startsWith("/") || path.startsWith("//") || /^[a-z][a-z0-9+.-]*:/i.test(path)) throw unsafePathError();
    const bare = path.startsWith(MOUNTED_PREFIX) ? path.slice(BASE.length - 1) : path;
    const pathname = bare.split(/[?#]/, 1)[0];
    if (!pathname.startsWith(API_PREFIX)) throw unsafePathError();
    for (const segment of pathname.split("/")) {
        const decoded = fullyDecode(segment);
        if (decoded === "." || decoded === ".." || decoded.includes("/") || decoded.includes("\\")) throw unsafePathError();
    }
    return `${BASE}${bare.slice(1)}`;
}

export function assetUrl(assetId: string) {
    return safeApiPath(`${API_PREFIX}assets/${encodeURIComponent(assetId)}`);
}

const defaultError = (status: number): Omit<ApiError, "request_id" | "phase"> => {
    if (status === 401) return { code: "unauthorized", message: "Authentication is required.", retryable: false };
    if (status === 403) return { code: "forbidden", message: "You are not allowed to perform this action.", retryable: false };
    if (status === 429) return { code: "rate_limited", message: "Too many requests. Please try again later.", retryable: true };
    if (status >= 500) return { code: "internal_error", message: "The service failed to process the request.", retryable: true };
    return { code: "request_failed", message: "The request could not be completed.", retryable: false };
};

const safeString = (value: unknown, fallback: string, pattern: RegExp) => typeof value === "string" && pattern.test(value) ? value : fallback;
const safeMessage = (value: unknown, fallback: string) => {
    if (typeof value !== "string" || value.length > 160) return fallback;
    if (value.includes("%")) return fallback;
    if (/[\\/]|[\r\n\t\u0000-\u001f]|(?:^|[^A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*:(?=\S)|\b\w[\w.-]*\.(?:ts|js|py|java|go|sql):\d+\b|api[_ -]?key|authorization|bearer|secret|token|traceback|stack|exception|error:|enoent|sqlite|postgres|mysql|internal\s+(?:server|database|error)/i.test(value)) return fallback;
    return value;
};

export class ApiRequestError extends Error implements ApiError {
    readonly code: string;
    readonly retryable: boolean;
    readonly request_id: string;
    readonly phase: string;
    readonly references?: Readonly<Partial<Record<"job" | "access" | "assignment" | "route" | "model", number>>>;

    constructor(details: ApiError, references?: Partial<Record<"job" | "access" | "assignment" | "route" | "model", number>>) {
        super(details.message);
        this.name = "ApiRequestError";
        this.code = details.code;
        this.retryable = details.retryable;
        this.request_id = details.request_id;
        this.phase = details.phase;
        this.references = references ? Object.freeze({ ...references }) : undefined;
    }
}

const safeReferences = (value: unknown) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
    const entries = Object.entries(value);
    const allowed = new Set(["job", "access", "assignment", "route", "model"]);
    if (entries.length > allowed.size || entries.some(([key, count]) => !allowed.has(key) || !Number.isSafeInteger(count) || (count as number) < 1 || (count as number) > 1_000_000)) return undefined;
    return Object.fromEntries(entries) as Partial<Record<"job" | "access" | "assignment" | "route" | "model", number>>;
};

async function responseError(response: Response): Promise<ApiRequestError> {
    const fallback = defaultError(response.status);
    const requestId = safeString(response.headers.get("x-request-id"), "", /^[A-Za-z0-9_-]{1,128}$/);
    const contentType = response.headers.get("content-type") || "";
    let payload: Record<string, unknown> | null = null;
    if (contentType.toLowerCase().includes("application/json")) {
        const value: unknown = await response.json().catch(() => null);
        if (value && typeof value === "object" && !Array.isArray(value)) payload = value as Record<string, unknown>;
    }
    const details: ApiError = {
        code: safeString(payload?.code, fallback.code, /^[a-z0-9_.-]{1,80}$/i),
        message: response.status >= 500 ? fallback.message : safeMessage(payload?.message, fallback.message),
        retryable: typeof payload?.retryable === "boolean" ? payload.retryable : fallback.retryable,
        request_id: safeString(payload?.request_id, requestId, /^[A-Za-z0-9_-]{1,128}$/),
        phase: safeString(payload?.phase, "response", /^[a-z0-9_.-]{1,80}$/i),
    };
    return new ApiRequestError(details, safeReferences(payload?.references));
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
    const safePath = safeApiPath(path);
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    const method = (init.method || "GET").toUpperCase();
    if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) headers.set("X-CSRF-Token", csrfToken);
    const response = await fetch(safePath, { ...init, credentials: "same-origin", headers });
    if (!response.ok) {
        throw await responseError(response);
    }
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
}
