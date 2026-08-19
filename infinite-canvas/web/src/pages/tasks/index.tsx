import { useEffect, useState } from "react";

import { fetchActivityJobs, type ActivityJob } from "@/api/activity";


export default function TasksPage() {
    const [jobs, setJobs] = useState<ActivityJob[] | null>(null);
    const [failed, setFailed] = useState(false);
    useEffect(() => { void fetchActivityJobs().then((value) => { setJobs(value); setFailed(false); }).catch(() => { setJobs([]); setFailed(true); }); }, []);

    return <section className="mx-auto max-w-6xl px-5 py-8"><p className="text-xs tracking-[0.2em] text-[#235fd6]">TASK CENTER</p><h1 className="mt-2 text-3xl font-semibold">任务中心</h1><p className="mt-2 text-sm text-[#687386]">刷新页面后仍可查询当前账号的任务状态。</p>
        <div className="mt-7 overflow-hidden rounded-xl border border-[#d9e0ea] bg-[#ffffff]">
            {jobs === null ? <p className="p-8 text-sm text-[#687386]">正在加载任务…</p> : failed ? <p role="alert" className="p-8 text-sm text-[#92400e]">任务暂时无法加载，请稍后重试。</p> : jobs.length === 0 ? <p className="p-8 text-sm text-[#687386]">暂无任务。进入项目后可以发起本地演示生成。</p> : <ul className="divide-y divide-[#d9e0ea]">{jobs.map((job) => <li key={job.id} className="grid grid-cols-[1fr_auto] gap-3 p-4"><div><div className="text-sm text-[#172033]">{job.operation}</div><div className="mt-1 text-xs text-[#8b95a7]">{job.service_id} · {job.id}</div></div><span className="text-xs text-[#6587d8]">{job.status}</span></li>)}</ul>}
        </div>
    </section>;
}
