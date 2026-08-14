import { useEffect, useState } from "react";

import { fetchAdminUsage, fetchUsage, fetchUsageRates, updateUsageRates, type AdminUsage, type Usage, type UsageRates } from "@/api/usage";
import { useSessionStore } from "@/stores/portal/use-session-store";

const formatFen = (fen: string) => {
    const value = BigInt(fen);
    const hundred = BigInt(100);
    return `¥${value / hundred}.${(value % hundred).toString().padStart(2, "0")}`;
};
const isYuanAmount = (value: string) => /^\d+(\.\d{1,2})?$/.test(value);
const yuanToFen = (value: string) => {
    const [yuan, decimal = ""] = value.split(".");
    return Number(yuan) * 100 + Number(decimal.padEnd(2, "0"));
};
const fenToYuan = (fen: number) => (fen / 100).toFixed(2);
const summaryText = (summary: Usage["summary"]) => `已完成任务 ${summary.successful_jobs} · 图片 ${summary.image_count} · 视频 ${summary.video_seconds} 秒 · ${formatFen(summary.total_cost_fen)}`;

function SummaryCards({ summary }: { summary: Usage["summary"] }) {
    const cards = [
        ["已完成任务", String(summary.successful_jobs)],
        ["生成图片", String(summary.image_count)],
        ["视频时长", `${summary.video_seconds} 秒`],
        ["估算费用", formatFen(summary.total_cost_fen)],
    ];
    return (
        <div className="mt-7 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {cards.map(([label, value]) => (
                <div key={label} className="rounded-xl border border-[#1f3f2a] bg-[#09120c] p-4">
                    <div className="text-xs text-[#829889]">{label}</div>
                    <div className="mt-2 text-2xl font-semibold text-[#e4f5e9]">{value}</div>
                </div>
            ))}
        </div>
    );
}

function ChargedJobs({ usage }: { usage: Usage }) {
    return (
        <section className="mt-7 overflow-hidden rounded-xl border border-[#1f3f2a] bg-[#09120c]">
            <div className="border-b border-[#183522] px-4 py-3">
                <h2 className="text-sm font-medium">已计费任务</h2>
            </div>
            {usage.jobs.length === 0 ? (
                <p className="p-6 text-sm text-[#829889]">暂无已计费的生成任务。</p>
            ) : (
                <ul className="divide-y divide-[#183522]">
                    {usage.jobs.map((job, index) => (
                        <li key={`${job.operation}-${job.charged_at}-${index}`} className="flex flex-wrap items-center justify-between gap-3 p-4">
                            <div>
                                <div className="text-sm text-[#e4f5e9]">{job.model_id ? `${job.model_id} · ${job.operation}` : job.operation}</div>
                                <div className="mt-1 text-xs text-[#688371]">
                                    {job.route_id ? `${job.route_id} · ` : ""}{job.image_count} 张图片 · {job.video_seconds} 秒视频 · {job.status}
                                </div>
                            </div>
                            <div className="text-sm font-medium text-[#58d881]">{formatFen(job.cost_fen)}</div>
                        </li>
                    ))}
                </ul>
            )}
        </section>
    );
}

function RateSettings({ rates, onSave }: { rates: UsageRates; onSave: (video: string, image: string) => Promise<boolean> }) {
    const [video, setVideo] = useState(fenToYuan(rates.video_price_fen));
    const [image, setImage] = useState(fenToYuan(rates.image_price_fen));
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        setVideo(fenToYuan(rates.video_price_fen));
        setImage(fenToYuan(rates.image_price_fen));
    }, [rates]);

    const save = async () => {
        if (!isYuanAmount(video) || !isYuanAmount(image)) {
            window.alert("价格必须是最多两位小数的非负金额。");
            return;
        }
        setSaving(true);
        try {
            if (!(await onSave(video, image))) {
                setVideo(fenToYuan(rates.video_price_fen));
                setImage(fenToYuan(rates.image_price_fen));
            }
        } finally {
            setSaving(false);
        }
    };

    return (
        <section className="mt-7 rounded-xl border border-[#1f3f2a] bg-[#09120c] p-5">
            <h2 className="text-sm font-medium">估算费率</h2>
            <p className="mt-1 text-xs text-[#829889]">价格按元输入，服务端以分保存；这是内部估算，不是供应商账单。</p>
            <div className="mt-4 grid gap-4 md:grid-cols-2">
                <label className="text-sm text-[#b9d0c0]">
                    每秒视频价格（元）
                    <input aria-label="每秒视频价格（元）" inputMode="decimal" value={video} onChange={(event) => setVideo(event.target.value)} className="mt-2 block w-full rounded-lg border border-[#285038] bg-[#08100b] px-3 py-2 text-[#e5f5e9]" />
                </label>
                <label className="text-sm text-[#b9d0c0]">
                    每张图片价格（元）
                    <input aria-label="每张图片价格（元）" inputMode="decimal" value={image} onChange={(event) => setImage(event.target.value)} className="mt-2 block w-full rounded-lg border border-[#285038] bg-[#08100b] px-3 py-2 text-[#e5f5e9]" />
                </label>
            </div>
            <button type="button" disabled={saving} onClick={() => void save()} className="mt-5 rounded-lg bg-[#47d978] px-4 py-2 text-sm font-medium text-[#041008] disabled:opacity-40">
                保存价格
            </button>
        </section>
    );
}

function AdminUsageDetails({ usage }: { usage: AdminUsage }) {
    return (
        <section className="mt-7 rounded-xl border border-[#1f3f2a] bg-[#09120c] p-5">
            <h2 className="text-sm font-medium">全部用户统计</h2>
            <p className="mt-2 text-sm text-[#829889]">全局汇总：{summaryText(usage.summary)}</p>
            <ul className="mt-4 divide-y divide-[#183522] border-y border-[#183522]">
                {usage.users.map((user) => (
                    <li key={user.user_id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                        <span className="text-sm text-[#e4f5e9]">
                            {user.user_id} · {summaryText(user.summary)}
                        </span>
                    </li>
                ))}
            </ul>
            <h3 className="mt-6 text-sm font-medium">全部已计费任务</h3>
            {usage.jobs.length === 0 ? (
                <p className="mt-3 text-sm text-[#829889]">暂无全局已计费任务。</p>
            ) : (
                <ul className="mt-3 divide-y divide-[#183522] border-y border-[#183522]">
                    {usage.jobs.map((job, index) => (
                        <li key={`${job.user_id}-${job.operation}-${job.charged_at}-${index}`} className="flex flex-wrap items-center justify-between gap-3 py-3">
                            <span>
                                <span className="block text-sm text-[#e4f5e9]">
                                    {job.user_id} · {job.model_id ? `${job.model_id} · ` : ""}{job.operation}
                                </span>
                                <span className="mt-1 block text-xs text-[#688371]">
                                    {job.route_id ? `${job.route_id} · ` : ""}{job.status} · {job.image_count} 张图片 · {job.video_seconds} 秒视频 · {job.charged_at}
                                </span>
                            </span>
                            <span className="text-xs text-[#58d881]">{formatFen(job.cost_fen)}</span>
                        </li>
                    ))}
                </ul>
            )}
        </section>
    );
}

export default function UsagePage() {
    const isAdmin = useSessionStore((state) => state.session?.role === "admin");
    const [usage, setUsage] = useState<Usage | null>(null);
    const [adminUsage, setAdminUsage] = useState<AdminUsage | null>(null);
    const [rates, setRates] = useState<UsageRates | null>(null);
    const [ownerFailed, setOwnerFailed] = useState(false);
    const [adminUsageFailed, setAdminUsageFailed] = useState(false);
    const [ratesFailed, setRatesFailed] = useState(false);

    useEffect(() => {
        let active = true;
        setOwnerFailed(false);
        setAdminUsageFailed(false);
        setRatesFailed(false);
        void fetchUsage()
            .then((value) => {
                if (active) {
                    setUsage(value);
                    setOwnerFailed(false);
                }
            })
            .catch(() => {
                if (active) {
                    setOwnerFailed(true);
                }
            });
        if (isAdmin) {
            void fetchAdminUsage()
                .then((nextAdminUsage) => {
                    if (active) {
                        setAdminUsage(nextAdminUsage);
                        setAdminUsageFailed(false);
                    }
                })
                .catch(() => {
                    if (active) setAdminUsageFailed(true);
                });
            void fetchUsageRates()
                .then((nextRates) => {
                    if (active) {
                        setRates(nextRates);
                        setRatesFailed(false);
                    }
                })
                .catch(() => {
                    if (active) setRatesFailed(true);
                });
        }
        return () => {
            active = false;
        };
    }, [isAdmin]);

    const saveRates = async (video: string, image: string) => {
        const videoPriceFen = yuanToFen(video);
        const imagePriceFen = yuanToFen(image);
        if (!Number.isSafeInteger(videoPriceFen) || !Number.isSafeInteger(imagePriceFen)) {
            window.alert("价格超出可保存范围。");
            return false;
        }
        try {
            const saved = await updateUsageRates(videoPriceFen, imagePriceFen);
            setRates(saved);
            return true;
        } catch {
            window.alert("价格未保存，请重试。");
            return false;
        }
    };

    return (
        <section className="mx-auto max-w-6xl px-5 py-8">
            <p className="text-xs tracking-[0.2em] text-[#58ed87]">USAGE · COST</p>
            <h1 className="mt-2 text-3xl font-semibold">生成统计</h1>
            <p className="mt-2 text-sm text-[#829889]">统计当前账号已完成的图片和视频任务；费用按管理员配置费率估算，不代表供应商账单。</p>
            {ownerFailed && (
                <p role="alert" className="mt-5 rounded border border-[#70502b] bg-[#241a0c] p-3 text-sm text-[#ffbd73]">
                    统计暂时无法完整加载，请稍后重试。
                </p>
            )}
            {adminUsageFailed && (
                <p role="alert" className="mt-5 rounded border border-[#70502b] bg-[#241a0c] p-3 text-sm text-[#ffbd73]">
                    全部用户统计暂时无法加载，请稍后重试。
                </p>
            )}
            {ratesFailed && (
                <p role="alert" className="mt-5 rounded border border-[#70502b] bg-[#241a0c] p-3 text-sm text-[#ffbd73]">
                    计费价格暂时无法加载，请稍后重试。
                </p>
            )}
            {usage === null && ownerFailed ? (
                <p className="mt-7 text-sm text-[#829889]">统计数据暂时不可用。</p>
            ) : usage === null ? (
                <p className="mt-7 text-sm text-[#829889]">正在加载统计…</p>
            ) : (
                <>
                    <SummaryCards summary={usage.summary} />
                    <ChargedJobs usage={usage} />
                </>
            )}
            {isAdmin && adminUsage && <AdminUsageDetails usage={adminUsage} />}
            {isAdmin && rates && <RateSettings rates={rates} onSave={saveRates} />}
        </section>
    );
}
