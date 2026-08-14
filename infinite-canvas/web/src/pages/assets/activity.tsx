import { useEffect, useState } from "react";

import { fetchActivityAssets, type ActivityAsset } from "@/api/activity";


export default function ActivityAssetsPage() {
    const [assets, setAssets] = useState<ActivityAsset[] | null>(null);
    const [failed, setFailed] = useState(false);
    useEffect(() => { void fetchActivityAssets().then((value) => { setAssets(value); setFailed(false); }).catch(() => { setAssets([]); setFailed(true); }); }, []);

    return <section className="mx-auto max-w-6xl px-5 py-8"><p className="text-xs tracking-[0.2em] text-[#58ed87]">ASSET LIBRARY</p><h1 className="mt-2 text-3xl font-semibold">资产库</h1><p className="mt-2 text-sm text-[#829889]">这里只显示当前账号已经登记到服务端的资产。</p>
        <div className="mt-7 overflow-hidden rounded-xl border border-[#1f3f2a] bg-[#09120c]">
            {assets === null ? <p className="p-8 text-sm text-[#829889]">正在加载资产…</p> : failed ? <p role="alert" className="p-8 text-sm text-[#ffbd73]">资产暂时无法加载，请稍后重试。</p> : assets.length === 0 ? <p className="p-8 text-sm text-[#829889]">暂无服务端资产。后续图片切片会开放上传与复用。</p> : <ul className="divide-y divide-[#183522]">{assets.map((asset) => <li key={asset.asset_id} className="grid grid-cols-[1fr_auto] gap-3 p-4"><div><div className="text-sm text-[#e4f5e9]">{asset.mime_type}</div><div className="mt-1 text-xs text-[#688371]">{asset.kind} · {asset.asset_id}</div></div><span className="text-xs text-[#58d881]">{asset.status}</span></li>)}</ul>}
        </div>
    </section>;
}
