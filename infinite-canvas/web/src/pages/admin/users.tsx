import { useEffect, useState } from "react";

import { fetchAdminUsers, setAdminUserEnabled, type AdminUser } from "@/api/admin";


export default function AdminUsersPage() {
    const [users, setUsers] = useState<AdminUser[] | null>(null);
    const [busyUser, setBusyUser] = useState<string | null>(null);
    const [failed, setFailed] = useState(false);

    useEffect(() => {
        void fetchAdminUsers().then((value) => { setUsers(value); setFailed(false); }).catch(() => { setUsers([]); setFailed(true); });
    }, []);

    const toggle = async (user: AdminUser) => {
        setBusyUser(user.user_id);
        try {
            const updated = await setAdminUserEnabled(user.user_id, !user.enabled);
            setUsers((current) => current?.map((item) => item.user_id === updated.user_id ? updated : item) || []);
        } catch {
            setFailed(true);
        } finally {
            setBusyUser(null);
        }
    };

    return <section className="mx-auto max-w-6xl px-5 py-8">
        <p className="text-xs tracking-[0.2em] text-[#6b92ed]">ADMIN · ACCOUNTS</p>
        <h1 className="mt-2 text-3xl font-semibold">账号管理</h1>
        <p className="mt-2 text-sm text-[#848a98]">管理员可以启用或停用账号。停用后，该账号现有登录会立即失效。</p>
        {failed && <p role="alert" className="mt-5 rounded border border-[#92400e] bg-[#fef3c7] p-3 text-sm text-[#92400e]">账号操作未完成，请重试。</p>}
        <div className="mt-7 overflow-hidden rounded-xl border border-[#222b3f] bg-[#ffffff]">
            {users === null ? <p className="p-8 text-sm text-[#848a98]">正在加载账号…</p> : <ul className="divide-y divide-[#1b2335]">{users.map((user) => <li key={user.user_id} className="flex flex-wrap items-center justify-between gap-4 p-4">
                <div><div className="text-sm text-[#e4f5e9]">{user.display_name} <span className="text-[#848a98]">· {user.username}</span></div><div className="mt-1 text-xs text-[#6b7283]">{user.role === "admin" ? "管理员" : "普通用户"} · {user.model_ids.length} 个模型</div></div>
                <div className="flex items-center gap-3"><span className={user.enabled ? "text-xs text-[#6587d8]" : "text-xs text-[#92400e]"}>{user.enabled ? "已启用" : "已停用"}</span><button type="button" disabled={busyUser === user.user_id} onClick={() => void toggle(user)} aria-label={`${user.enabled ? "停用" : "启用"} ${user.username}`} className="rounded border border-[#344365] px-3 py-1.5 text-xs text-[#bcc4d7] hover:bg-[#121826] disabled:opacity-50">{user.enabled ? "停用" : "启用"}</button></div>
            </li>)}</ul>}
        </div>
    </section>;
}
