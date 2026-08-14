import { CircleCheck, CircleX, LoaderCircle, X } from "lucide-react";

import { dismissGenerationTask, useGenerationTasks } from "@/features/generation/use-generation-job";


const statusLabel = { idle: "等待", submitting: "提交中", queued: "排队中", running: "生成中", succeeded: "已完成", failed: "失败" } as const;

export function TaskTray() {
    const tasks = useGenerationTasks((state) => state.tasks);
    return (
        <aside data-testid="task-tray" aria-label="任务托盘" className="fixed inset-x-0 bottom-0 z-40 flex h-[var(--task-tray-height)] items-center gap-3 overflow-x-auto border-t border-[#21472f] bg-[#09110c]/98 px-4 text-sm text-[#89a792] backdrop-blur md:left-56">
            <span className="shrink-0 font-medium text-[#dceee1]">运行任务</span>
            {tasks.length === 0 ? <span>暂无运行任务</span> : tasks.map((task) => {
                const terminal = task.status === "succeeded" || task.status === "failed";
                const Icon = task.status === "succeeded" ? CircleCheck : task.status === "failed" ? CircleX : LoaderCircle;
                return <div key={task.jobId} className="flex shrink-0 items-center gap-2 rounded-md border border-[#20452e] bg-[#0d1b12] px-2.5 py-1 text-xs" title={task.title}><Icon className={`size-3.5 ${terminal ? "" : "animate-spin"}`} /><span className="max-w-32 truncate">{task.title || task.jobId}</span><span className="text-[#64df88]">{statusLabel[task.status]}</span>{terminal ? <button aria-label={`移除任务 ${task.jobId}`} onClick={() => dismissGenerationTask(task.jobId)}><X className="size-3" /></button> : null}</div>;
            })}
        </aside>
    );
}
