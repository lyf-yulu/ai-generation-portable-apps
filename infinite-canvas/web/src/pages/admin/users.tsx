import { useEffect, useState } from "react";

import { approveAdminRegistration, fetchAdminRegistrations, fetchAdminUsers, rejectAdminRegistration, setAdminUserEnabled, type AdminRegistration, type AdminUser } from "@/api/admin";
import { ChangePasswordDialog } from "@/components/auth/change-password-dialog";
import { UserPasswordDialog } from "@/components/admin/user-password-dialog";
import { useSessionStore } from "@/stores/portal/use-session-store";


export default function AdminUsersPage() {
    const session = useSessionStore((state) => state.session);
    const [users, setUsers] = useState<AdminUser[] | null>(null);
    const [registrations, setRegistrations] = useState<AdminRegistration[] | null>(null);
    const [busyUser, setBusyUser] = useState<string | null>(null);
    const [busyRegistration, setBusyRegistration] = useState<string | null>(null);
    const [failed, setFailed] = useState(false);
    const [selfPasswordOpen, setSelfPasswordOpen] = useState(false);
    const [passwordUser, setPasswordUser] = useState<AdminUser | null>(null);

    const load = () => {
        void Promise.all([fetchAdminUsers(), fetchAdminRegistrations()])
            .then(([nextUsers, nextRegistrations]) => {
                setUsers(nextUsers);
                setRegistrations(nextRegistrations);
                setFailed(false);
            })
            .catch(() => { setUsers([]); setRegistrations([]); setFailed(true); });
    };

    useEffect(load, []);

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

    const actOnRegistration = async (registration: AdminRegistration, action: "approve" | "reject") => {
        setBusyRegistration(registration.user_id);
        try {
            if (action === "approve") {
                await approveAdminRegistration(registration.user_id);
            } else {
                await rejectAdminRegistration(registration.user_id);
            }
            load();
        } catch {
            setFailed(true);
        } finally {
            setBusyRegistration(null);
        }
    };

    const approvedUsers = users?.filter((user) => user.approval_status === "approved") ?? null;

    return <section className="mx-auto max-w-6xl px-5 py-8">
        <p className="text-xs tracking-[0.2em] text-[#58ed87]">ADMIN · ACCOUNTS</p>
        <h1 className="mt-2 text-3xl font-semibold">账号管理</h1>
        <p className="mt-2 text-sm text-[#829889]">管理员可以启用或停用账号、为普通用户设置密码，并审核自助注册的新账号。停用后，该账号现有登录会立即失效。</p>
        {failed && <p role="alert" className="mt-5 rounded border border-[#70502b] bg-[#241a0c] p-3 text-sm text-[#ffbd73]">账号操作未完成，请重试。</p>}
        <div className="mt-7 overflow-hidden rounded-xl border border-[#1f3f2a] bg-[#09120c]">
            <h2 className="border-b border-[#183522] p-4 text-sm font-semibold text-[#e4f5e9]">待审核注册</h2>
            {registrations === null ? <p className="p-8 text-sm text-[#829889]">正在加载待审核注册…</p> : registrations.length === 0 ? <p className="p-8 text-sm text-[#829889]">暂无待审核注册。</p> : <ul className="divide-y divide-[#183522]">{registrations.map((registration) => <li key={registration.user_id} className="flex flex-wrap items-center justify-between gap-4 p-4">
                <div><div className="text-sm text-[#e4f5e9]">{registration.display_name} <span className="text-[#829889]">· {registration.username}</span></div><div className="mt-1 text-xs text-[#688371]">申请于 {new Date(registration.created_at).toLocaleString()}</div></div>
                <div className="flex items-center gap-3"><button type="button" disabled={busyRegistration === registration.user_id} onClick={() => void actOnRegistration(registration, "approve")} aria-label={`通过 ${registration.username}`} className="rounded border border-[#2f6542] px-3 py-1.5 text-xs text-[#b9d7c2] hover:bg-[#102619] disabled:opacity-50">通过</button><button type="button" disabled={busyRegistration === registration.user_id} onClick={() => void actOnRegistration(registration, "reject")} aria-label={`拒绝 ${registration.username}`} className="rounded border border-[#65322f] px-3 py-1.5 text-xs text-[#d7bcb9] hover:bg-[#261210] disabled:opacity-50">拒绝</button></div>
            </li>)}</ul>}
        </div>
        <div className="mt-7 overflow-hidden rounded-xl border border-[#1f3f2a] bg-[#09120c]">
            <h2 className="border-b border-[#183522] p-4 text-sm font-semibold text-[#e4f5e9]">账号列表</h2>
            {approvedUsers === null ? <p className="p-8 text-sm text-[#829889]">正在加载账号…</p> : <ul className="divide-y divide-[#183522]">{approvedUsers.map((user) => <li key={user.user_id} className="flex flex-wrap items-center justify-between gap-4 p-4">
                <div><div className="text-sm text-[#e4f5e9]">{user.display_name} <span className="text-[#829889]">· {user.username}</span></div><div className="mt-1 text-xs text-[#688371]">{user.role === "admin" ? "管理员" : "普通用户"} · {user.model_ids.length} 个模型</div></div>
                <div className="flex items-center gap-3">{user.user_id === session?.user_id ? <button type="button" onClick={() => setSelfPasswordOpen(true)} aria-label={`修改密码 ${user.username}`} className="rounded border border-[#2f6542] px-3 py-1.5 text-xs text-[#b9d7c2] hover:bg-[#102619]">修改密码</button> : user.role === "user" ? <button type="button" onClick={() => setPasswordUser(user)} aria-label={`设置密码 ${user.username}`} className="rounded border border-[#2f6542] px-3 py-1.5 text-xs text-[#b9d7c2] hover:bg-[#102619]">设置密码</button> : null}<span className={user.enabled ? "text-xs text-[#58d881]" : "text-xs text-[#ffbd73]"}>{user.enabled ? "已启用" : "已停用"}</span><button type="button" disabled={busyUser === user.user_id} onClick={() => void toggle(user)} aria-label={`${user.enabled ? "停用" : "启用"} ${user.username}`} className="rounded border border-[#2f6542] px-3 py-1.5 text-xs text-[#b9d7c2] hover:bg-[#102619] disabled:opacity-50">{user.enabled ? "停用" : "启用"}</button></div>
            </li>)}</ul>}
        </div>
        <ChangePasswordDialog open={selfPasswordOpen} onClose={() => setSelfPasswordOpen(false)} />
        {passwordUser && <UserPasswordDialog user={passwordUser} onClose={() => setPasswordUser(null)} onUpdated={(updated) => setUsers((current) => current?.map((item) => item.user_id === updated.user_id ? updated : item) || [])} />}
    </section>;
}
