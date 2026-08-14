import { useEffect, useState } from "react";

import { fetchAdminUsage, type AdminUsageCounters, type AdminUserUsage } from "@/api/admin";

const empty: AdminUsageCounters = { jobs: 0, succeeded: 0, failed: 0, active: 0, image: 0, video: 0 };

export default function AdminUsagePage() {
    const [totals, setTotals] = useState<AdminUsageCounters>(empty);
    const [users, setUsers] = useState<AdminUserUsage[] | null>(null);
    const [failed, setFailed] = useState(false);
    useEffect(() => {
        let active = true;
        void fetchAdminUsage().then((result) => {
            if (!active) return;
            setTotals(result.totals); setUsers(result.users); setFailed(false);
        }).catch(() => { if (active) { setUsers([]); setFailed(true); } });
        return () => { active = false; };
    }, []);
    const cards = [["全部任务", totals.jobs], ["成功", totals.succeeded], ["进行中", totals.active], ["失败", totals.failed], ["图像", totals.image], ["视频", totals.video]] as const;
    return <section className="mx-auto max-w-7xl px-4 py-7 sm:px-5">
        <p className="text-xs tracking-[0.2em] text-[#58ed87]">ADMIN · USAGE</p>
        <h1 className="mt-2 text-2xl font-semibold sm:text-3xl">使用统计</h1>
        <p className="mt-2 text-sm text-[#829889]">按服务端确认的用户 ID 汇总任务，不采用浏览器上报的用户名或所有者。</p>
        {failed && <p role="alert" className="mt-5 rounded border border-[#70502b] bg-[#241a0c] p-3 text-sm text-[#ffbd73]">统计加载失败，请重试。</p>}
        <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">{cards.map(([label, value]) => <div key={label} className="rounded-xl border border-[#1f4c2e] bg-[#09150d] p-4"><span className="block text-xs text-[#829889]">{label}</span><strong className="mt-2 block text-2xl text-[#dff6e6]">{value}</strong></div>)}</div>
        <div className="mt-6 overflow-x-auto rounded-xl border border-[#1f3f2a] bg-[#09120c]">
            {users === null ? <p className="p-8 text-sm text-[#829889]">正在加载统计…</p> : <table className="w-full min-w-[46rem] text-left text-sm"><thead className="border-b border-[#1f3f2a] text-xs text-[#829889]"><tr><th className="p-4">账号</th><th>任务</th><th>图像</th><th>视频</th><th>成功</th><th>进行中</th><th>失败</th></tr></thead><tbody className="divide-y divide-[#183522]">{users.map((user) => <tr key={user.user_id}><th className="p-4 font-medium text-[#e4f5e9]">{user.display_name}<span className="ml-2 font-normal text-[#829889]">· {user.username}</span></th><td>{user.jobs}</td><td>{user.image}</td><td>{user.video}</td><td>{user.succeeded}</td><td>{user.active}</td><td>{user.failed}</td></tr>)}</tbody></table>}
        </div>
    </section>;
}
