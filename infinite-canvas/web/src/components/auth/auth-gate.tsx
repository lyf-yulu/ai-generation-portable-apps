import { useEffect, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useSessionStore } from "@/stores/portal/use-session-store";


export function AuthGate({ children }: { children: ReactNode }) {
    const location = useLocation();
    const session = useSessionStore((state) => state.session);
    const loading = useSessionStore((state) => state.loading);
    const errorCode = useSessionStore((state) => state.errorCode);
    const loadSession = useSessionStore((state) => state.loadSession);

    useEffect(() => {
        if (!session && !loading && !errorCode) void loadSession("local").catch(() => undefined);
    }, [errorCode, loadSession, loading, session]);

    if (loading || (!session && !errorCode)) {
        return <main className="flex h-dvh items-center justify-center bg-[#050806] text-sm text-[#9bb3a2]">正在验证登录状态…</main>;
    }
    if (!session && errorCode && !["AUTH_REQUIRED", "unauthorized"].includes(errorCode)) {
        return <main className="flex h-dvh flex-col items-center justify-center gap-3 bg-[#050806] text-[#dceee1]"><p>暂时无法验证登录状态</p><button className="rounded border border-[#2f6542] px-3 py-2 text-sm text-[#63ed8d]" onClick={() => useSessionStore.setState({ errorCode: null })}>重新连接</button></main>;
    }
    if (!session) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
    if (session.must_change_password && location.pathname !== "/login") {
        return <Navigate to="/login" replace state={{ from: location.pathname, changePassword: true }} />;
    }
    return <>{children}</>;
}

export function RoleGate({ allowed, children }: { allowed: Array<"admin" | "user" | "viewer">; children: ReactNode }) {
    const session = useSessionStore((state) => state.session);
    if (!session || !allowed.includes(session.role)) {
        return <main className="flex h-full items-center justify-center bg-[#050806] text-[#9bb3a2]">页面不存在</main>;
    }
    return <>{children}</>;
}
