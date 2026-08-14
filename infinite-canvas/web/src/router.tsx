import { createBrowserRouter, Outlet } from "react-router-dom";

import { AuthGate } from "@/components/auth/auth-gate";
import { ProductShell } from "@/components/layout/product-shell";
import ActivityAssetsPage from "@/pages/assets/activity";
import CanvasPage from "@/pages/canvas";
import CanvasProjectPage from "@/pages/canvas/project";
import NotFound from "@/pages/not-found";
import TasksPage from "@/pages/tasks";

// 登录 / 账号管理 / 模型派发 / 用量统计等页面已移除：
// 画布挂在 Portal 的 /infinite-canvas/ 下，身份、账号与统计由 Portal 统一负责，
// 保留两套会让用户看到两份对不上的数字。AuthGate 必须保留 —— 它不只是路由守卫，
// 还负责 setStorageScope()，决定本地 IndexedDB 的库名。
const routes = [
    {
        element: <AuthGate><ProductShell><Outlet /></ProductShell></AuthGate>,
        children: [
            { path: "/", element: <CanvasPage /> },
            { path: "/canvas", element: <CanvasPage /> },
            { path: "/canvas/:id", element: <CanvasProjectPage /> },
            { path: "/assets", element: <ActivityAssetsPage /> },
            { path: "/tasks", element: <TasksPage /> },
        ],
    },
    { path: "*", element: <NotFound /> },
];

// basename 让 react-router 认识 /infinite-canvas/ 前缀，否则刷新页面会落到 NotFound。
export const router = createBrowserRouter(routes, {
    basename: import.meta.env.BASE_URL,
});
