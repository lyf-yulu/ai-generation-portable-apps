import { ApiRequestError } from "@/api/client";
import type { ApiError } from "@/api/contracts";

/** Convert only structured portal errors to concise, non-sensitive UI text. */
export function generationErrorMessage(error: unknown) {
    const details = error instanceof ApiRequestError ? error : null;
    if (!details) return "网络连接中断，任务可能仍在排队。请使用同一次操作重试。";
    if (details.code === "unauthorized") return "登录已失效，请重新登录后再试。";
    if (details.code === "forbidden") return "你没有使用此资源的权限。";
    if (details.code === "rate_limited") return "请求过于频繁，请稍后重试。";
    if (details.code === "internal_error") return "服务暂时不可用，请稍后重试。";
    return details.message;
}

export function safeFailureMetadata(error: unknown): Pick<ApiError, "code" | "request_id" | "phase"> | undefined {
    if (!(error instanceof ApiRequestError)) return undefined;
    return { code: error.code, request_id: error.request_id, phase: error.phase };
}
