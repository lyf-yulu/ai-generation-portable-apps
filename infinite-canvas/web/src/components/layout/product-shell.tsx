import { Boxes, FolderKanban, Images, Orbit, Rows3 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";

import { TaskTray } from "@/components/layout/task-tray";
import { useSessionStore } from "@/stores/portal/use-session-store";


// 「统计」与账号/模型派发管理组已移除：Portal 的「统计」tab 是唯一口径，
// 模型来自 seedance/nano-banana 的 providers.json，不在画布内派发。
// 工作流库（ComfyUI）是画布自有的管理员功能，保留且仅管理员可见。
const releasedNavigation = [
    { label: "项目", to: "/canvas", icon: FolderKanban },
    { label: "资产", to: "/assets", icon: Images },
    { label: "任务", to: "/tasks", icon: Rows3 },
] as const;

const adminNavigation = [
    { label: "工作流库", to: "/admin/comfy-workflows", icon: Boxes },
] as const;

export function ProductShell({ children }: { children: ReactNode }) {
    const session = useSessionStore((state) => state.session);
    const location = useLocation();
    const currentCanvasPath = /^\/canvas\/[^/]+$/.test(location.pathname) ? location.pathname : null;
    const [rememberedCanvas, setRememberedCanvas] = useState(() => ({ userId: session?.user_id ?? null, path: currentCanvasPath ?? "/canvas", ignoredPath: null as string | null }));
    const sameUser = rememberedCanvas.userId === (session?.user_id ?? null);
    const projectTarget = sameUser && currentCanvasPath !== rememberedCanvas.ignoredPath ? currentCanvasPath ?? rememberedCanvas.path : sameUser ? rememberedCanvas.path : "/canvas";
    const navigation = releasedNavigation.map((item) => item.to === "/canvas" ? { ...item, to: projectTarget } : item);
    const navItems = session?.role === "admin" ? [...navigation, ...adminNavigation] : navigation;

    useEffect(() => {
        const userId = session?.user_id ?? null;
        setRememberedCanvas((current) => {
            if (current.userId !== userId) return { userId, path: "/canvas", ignoredPath: currentCanvasPath };
            if (!currentCanvasPath) return current.ignoredPath ? { ...current, ignoredPath: null } : current;
            if (currentCanvasPath === current.ignoredPath || current.path === currentCanvasPath) return current;
            return { userId, path: currentCanvasPath, ignoredPath: null };
        });
    }, [currentCanvasPath, session?.user_id]);

    // 配色统一走主题变量（globals.css 的 :root / .dark），不写死色值 ——
    // 这样切换主题与日后改色都不需要动组件。
    return (
        <div className="h-dvh overflow-hidden bg-background text-foreground">
            <header className="flex h-14 items-center justify-between gap-2 overflow-x-auto border-b border-border bg-card px-3 md:hidden">
                <div className="flex shrink-0 items-center gap-2 font-semibold"><Orbit className="size-5 text-primary" /><span className="hidden sm:inline">无限画布</span></div>
                <nav className="flex shrink-0 gap-1">{navItems.map(({ label, to }) => <NavLink key={label} to={to} className="rounded px-2 py-1 text-xs text-muted-foreground">{label}</NavLink>)}</nav>
            </header>
            <aside aria-label="侧边栏" className="fixed inset-y-0 left-0 z-30 hidden w-56 border-r border-border bg-card p-4 md:flex md:flex-col">
                <div className="flex items-center gap-2 text-base font-semibold"><Orbit className="size-5 text-primary" /><span>无限画布</span></div>
                <nav className="mt-6 space-y-1" aria-label="主导航">
                    {navItems.map(({ label, to, icon: Icon }) => <NavLink key={label} to={to} className={({ isActive }) => `flex items-center gap-3 rounded-lg border-l-2 px-3 py-2.5 text-sm transition-colors ${isActive ? "border-primary bg-accent text-accent-foreground" : "border-transparent text-muted-foreground hover:bg-muted hover:text-foreground"}`}><Icon className="size-4" />{label}</NavLink>)}
                </nav>
                <div className="mt-auto border-t border-border pt-4">
                    <div className="text-sm text-foreground">{session?.username || "未登录"}</div>
                    <div className="mt-1 text-xs text-muted-foreground">{session?.role === "admin" ? "管理员" : "普通用户"}</div>
                </div>
            </aside>
            <main data-testid="product-main" className="h-[calc(100dvh-3.5rem)] overflow-auto bg-background pb-[var(--task-tray-height)] md:ml-56 md:h-dvh">{children}</main>
            <TaskTray />
        </div>
    );
}
