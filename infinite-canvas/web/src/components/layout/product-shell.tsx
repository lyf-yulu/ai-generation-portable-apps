import { FolderKanban, Images, Orbit, Rows3 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";

import { TaskTray } from "@/components/layout/task-tray";
import { useSessionStore } from "@/stores/portal/use-session-store";


// 「统计」与整个管理组已移除：Portal 的「统计」tab 是唯一口径，
// 模型来自 seedance/nano-banana 的 providers.json，不在画布内派发。
const releasedNavigation = [
    { label: "项目", to: "/canvas", icon: FolderKanban },
    { label: "资产", to: "/assets", icon: Images },
    { label: "任务", to: "/tasks", icon: Rows3 },
] as const;

export function ProductShell({ children }: { children: ReactNode }) {
    const session = useSessionStore((state) => state.session);
    const location = useLocation();
    const currentCanvasPath = /^\/canvas\/[^/]+$/.test(location.pathname) ? location.pathname : null;
    const [rememberedCanvas, setRememberedCanvas] = useState(() => ({ userId: session?.user_id ?? null, path: currentCanvasPath ?? "/canvas", ignoredPath: null as string | null }));
    const sameUser = rememberedCanvas.userId === (session?.user_id ?? null);
    const projectTarget = sameUser && currentCanvasPath !== rememberedCanvas.ignoredPath ? currentCanvasPath ?? rememberedCanvas.path : sameUser ? rememberedCanvas.path : "/canvas";
    const navigation = releasedNavigation.map((item) => item.to === "/canvas" ? { ...item, to: projectTarget } : item);

    useEffect(() => {
        const userId = session?.user_id ?? null;
        setRememberedCanvas((current) => {
            if (current.userId !== userId) return { userId, path: "/canvas", ignoredPath: currentCanvasPath };
            if (!currentCanvasPath) return current.ignoredPath ? { ...current, ignoredPath: null } : current;
            if (currentCanvasPath === current.ignoredPath || current.path === currentCanvasPath) return current;
            return { userId, path: currentCanvasPath, ignoredPath: null };
        });
    }, [currentCanvasPath, session?.user_id]);

    return (
        <div className="h-dvh overflow-hidden bg-[#050806] text-[#e5f5e9]">
            <header className="flex h-14 items-center justify-between gap-2 overflow-x-auto border-b border-[#193523] bg-[#08100b] px-3 md:hidden">
                <div className="flex shrink-0 items-center gap-2 font-semibold"><Orbit className="size-5 text-[#57ed86]" /><span className="hidden sm:inline">AI 创作画布</span></div>
                <nav className="flex shrink-0 gap-1">{navigation.map(({ label, to }) => <NavLink key={label} to={to} className="rounded px-2 py-1 text-xs text-[#a8bbae]">{label}</NavLink>)}</nav>
            </header>
            <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 border-r border-[#193523] bg-[#08100b] p-4 md:flex md:flex-col">
                <div className="flex items-center gap-2 text-base font-semibold"><Orbit className="size-5 text-[#57ed86]" /><span><i className="not-italic text-[#57ed86]">AI</i> 创作画布</span></div>
                <p className="mt-2 text-xs text-[#688371]">本地创作工作室</p>
                <nav className="mt-8 space-y-1" aria-label="主导航">
                    {navigation.map(({ label, to, icon: Icon }) => <NavLink key={label} to={to} className={({ isActive }) => `flex items-center gap-3 rounded-lg border-l-2 px-3 py-2.5 text-sm ${isActive ? "border-[#58ed87] bg-[#102619] text-[#e9fff0]" : "border-transparent text-[#94aa9a] hover:bg-[#0d1b12] hover:text-[#dceee1]"}`}><Icon className="size-4" />{label}</NavLink>)}
                </nav>
                <div className="mt-auto border-t border-[#193523] pt-4">
                    <div className="text-sm text-[#d8eadd]">{session?.username || "未登录"}</div>
                    <div className="mt-1 text-xs text-[#688371]">{session?.role === "admin" ? "管理员" : "普通用户"}</div>
                </div>
            </aside>
            <main data-testid="product-main" className="h-[calc(100dvh-3.5rem)] overflow-auto bg-[#050806] pb-[var(--task-tray-height)] md:ml-56 md:h-dvh">{children}</main>
            <TaskTray />
        </div>
    );
}
