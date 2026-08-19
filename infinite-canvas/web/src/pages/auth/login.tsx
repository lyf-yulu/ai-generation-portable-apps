import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useSessionStore } from "@/stores/portal/use-session-store";


type LoginLocationState = { from?: string; changePassword?: boolean };

export default function LoginPage() {
    const location = useLocation();
    const navigate = useNavigate();
    const session = useSessionStore((state) => state.session);
    const login = useSessionStore((state) => state.login);
    const changePassword = useSessionStore((state) => state.changePassword);
    const loading = useSessionStore((state) => state.loading);
    const [username, setUsername] = useState("");
    const [password, setPassword] = useState("");
    const [newPassword, setNewPassword] = useState("");
    const [message, setMessage] = useState("");
    const state = (location.state || {}) as LoginLocationState;
    const changing = Boolean(session?.must_change_password || state.changePassword);
    const destination = state.from && state.from.startsWith("/") && !state.from.startsWith("//") ? state.from : "/";

    const submit = async (event: FormEvent) => {
        event.preventDefault();
        setMessage("");
        try {
            if (changing) {
                await changePassword(password, newPassword);
            } else {
                const next = await login(username, password, "local");
                if (next.must_change_password) return;
            }
            navigate(destination, { replace: true });
        } catch {
            setMessage(changing ? "密码修改失败，请检查当前密码和新密码。" : "用户名或密码不正确。" );
        }
    };

    return (
        <main className="flex min-h-dvh items-center justify-center bg-[#050806] px-5 text-[#e5f5e9]">
            <section className="w-full max-w-sm rounded-2xl border border-[#21472f] bg-[#0a130d] p-7 shadow-2xl">
                <p className="text-xs tracking-[0.25em] text-[#55e986]">AI CREATION CANVAS</p>
                <h1 className="mt-3 text-2xl font-semibold">{changing ? "设置新密码" : "登录 AI 创作画布"}</h1>
                <p className="mt-2 text-sm text-[#8da995]">{changing ? "首次登录必须更换一次性密码。" : "使用管理员派发的本地账号。"}</p>
                <form className="mt-6 space-y-4" onSubmit={(event) => void submit(event)}>
                    {!changing ? <label className="block text-sm">用户名<input aria-label="用户名" autoComplete="username" className="mt-1 w-full rounded-lg border border-[#284d35] bg-[#071009] px-3 py-2 text-[#ecfff1] outline-none focus:border-[#59ed87]" value={username} onChange={(event) => setUsername(event.target.value)} /></label> : null}
                    <label className="block text-sm">{changing ? "当前密码" : "密码"}<input aria-label={changing ? "当前密码" : "密码"} type="password" autoComplete={changing ? "current-password" : "current-password"} className="mt-1 w-full rounded-lg border border-[#284d35] bg-[#071009] px-3 py-2 text-[#ecfff1] outline-none focus:border-[#59ed87]" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
                    {changing ? <label className="block text-sm">新密码<input aria-label="新密码" type="password" autoComplete="new-password" minLength={12} className="mt-1 w-full rounded-lg border border-[#284d35] bg-[#071009] px-3 py-2 text-[#ecfff1] outline-none focus:border-[#59ed87]" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></label> : null}
                    {message ? <p role="alert" className="text-sm text-[#ffbd73]">{message}</p> : null}
                    <button type="submit" disabled={loading || (!changing && (!username || !password)) || (changing && (!password || newPassword.length < 12))} className="w-full rounded-lg bg-[#54e982] px-4 py-2.5 font-semibold text-[#041108] disabled:opacity-45">{loading ? "处理中…" : changing ? "保存新密码" : "登录"}</button>
                </form>
                {!changing ? <p className="mt-5 text-center text-sm text-[#8da995]">没有账号？<Link to="/register" className="text-[#55e986] hover:underline">注册</Link></p> : null}
            </section>
        </main>
    );
}
