import { useEffect, useRef, useState } from "react";
import { Button, Input, Select, Switch } from "antd";
import dayjs from "dayjs";
import { saveAs } from "file-saver";

import { downloadAdminLog, fetchAdminLogContent, fetchAdminLogFiles, type AdminLogContent, type AdminLogFile } from "@/api/admin";
import { formatBytes } from "@/lib/image-utils";

const LEVEL_OPTIONS = [
    { label: "全部", value: "" },
    { label: "Debug", value: "DEBUG" },
    { label: "Info", value: "INFO" },
    { label: "警告", value: "WARNING" },
    { label: "错误", value: "ERROR" },
    { label: "严重", value: "CRITICAL" },
];

export default function AdminLogsPage() {
    const [files, setFiles] = useState<AdminLogFile[] | null>(null);
    const [failed, setFailed] = useState(false);
    const [selectedFile, setSelectedFile] = useState<string | null>(null);
    const [level, setLevel] = useState("");
    const [keywordInput, setKeywordInput] = useState("");
    const [keyword, setKeyword] = useState("");
    const [content, setContent] = useState<AdminLogContent | null>(null);
    const [contentLoading, setContentLoading] = useState(false);
    const [contentFailed, setContentFailed] = useState(false);
    const [autoRefresh, setAutoRefresh] = useState(false);
    const [refreshTick, setRefreshTick] = useState(0);
    const [exporting, setExporting] = useState(false);
    const [exportFailed, setExportFailed] = useState(false);
    const loadingRef = useRef(false);

    useEffect(() => {
        let active = true;
        void fetchAdminLogFiles()
            .then((list) => {
                if (!active) return;
                setFiles(list);
                setFailed(false);
                setSelectedFile((current) => (current && list.some((item) => item.name === current) ? current : list[0]?.name ?? null));
            })
            .catch(() => {
                if (active) setFailed(true);
            });
        return () => {
            active = false;
        };
    }, [refreshTick]);

    useEffect(() => {
        if (!selectedFile) return;
        let active = true;
        loadingRef.current = true;
        setContentLoading(true);
        setContentFailed(false);
        void fetchAdminLogContent(selectedFile, { lines: 500, level: level || undefined, q: keyword || undefined })
            .then((result) => {
                if (!active) return;
                setContent(result);
                setContentFailed(false);
            })
            .catch(() => {
                if (active) setContentFailed(true);
            })
            .finally(() => {
                if (active) {
                    setContentLoading(false);
                    loadingRef.current = false;
                }
            });
        return () => {
            active = false;
            loadingRef.current = false;
        };
    }, [selectedFile, level, keyword, refreshTick]);

    useEffect(() => {
        if (!autoRefresh) return;
        const timer = setInterval(() => {
            if (loadingRef.current) return;
            setRefreshTick((tick) => tick + 1);
        }, 5000);
        return () => clearInterval(timer);
    }, [autoRefresh]);

    const search = () => setKeyword(keywordInput.trim());
    const exportLog = () => {
        if (!selectedFile || exporting) return;
        setExporting(true);
        setExportFailed(false);
        void downloadAdminLog(selectedFile)
            .then(({ blob, filename }) => {
                saveAs(blob, filename);
                setExporting(false);
            })
            .catch(() => {
                setExporting(false);
                setExportFailed(true);
            });
    };

    return (
        <section className="mx-auto max-w-6xl px-5 py-8">
            <p className="text-xs tracking-[0.2em] text-[#58ed87]">ADMIN · LOGS</p>
            <h1 className="mt-2 text-2xl font-semibold sm:text-3xl">后台日志</h1>
            <p className="mt-2 text-sm text-[#829889]">查看服务器运行日志，支持筛选与导出。</p>
            {failed && <p role="alert" className="mt-5 rounded border border-[#70502b] bg-[#241a0c] p-3 text-sm text-[#ffbd73]">日志加载失败，请重试。</p>}
            {exportFailed && <p role="alert" className="mt-3 rounded border border-[#70502b] bg-[#241a0c] p-3 text-sm text-[#ffbd73]">日志导出失败，请重试。</p>}
            <div className="mt-6 flex flex-wrap items-center gap-2">
                <Select
                    aria-label="日志文件"
                    className="min-w-56"
                    value={selectedFile ?? undefined}
                    placeholder="暂无日志文件"
                    options={(files ?? []).map((item) => ({
                        label: `${item.name} · ${formatBytes(item.size)} · ${dayjs(item.mtime * 1000).format("MM-DD HH:mm")}`,
                        value: item.name,
                    }))}
                    onChange={(value) => setSelectedFile(value)}
                />
                <Select aria-label="级别" className="w-28" value={level} options={LEVEL_OPTIONS} onChange={(value) => setLevel(value)} />
                <Input
                    aria-label="关键词"
                    className="w-52"
                    allowClear
                    maxLength={200}
                    placeholder="搜索关键词"
                    value={keywordInput}
                    onChange={(event) => setKeywordInput(event.target.value)}
                    onPressEnter={search}
                />
                <Button onClick={search}>搜索</Button>
                <Button onClick={() => setRefreshTick((tick) => tick + 1)}>刷新</Button>
                <span className="flex items-center gap-2 text-xs text-[#829889]">
                    自动刷新
                    <Switch checked={autoRefresh} onChange={setAutoRefresh} />
                </span>
                <Button loading={exporting} disabled={!selectedFile} onClick={exportLog}>
                    导出
                </Button>
            </div>
            <div className="mt-4 rounded-xl border border-[#1f3f2a] bg-[#09120c]">
                <div className="flex flex-wrap items-center gap-3 border-b border-[#1f3f2a] px-4 py-3 text-xs text-[#829889]">
                    <span className="text-[#dff6e6]">{content?.file ?? selectedFile ?? "—"}</span>
                    {content && (
                        <span>
                            窗口 {content.window_total} 行 · 匹配 {content.log_lines.length} 行
                            {content.truncated ? " · 内容过长已截断" : ""}
                        </span>
                    )}
                    {content && <span>更新于 {dayjs().format("HH:mm:ss")}</span>}
                </div>
                {files === null ? (
                    <p className="p-8 text-sm text-[#829889]">正在加载日志…</p>
                ) : !selectedFile ? (
                    <p className="p-8 text-sm text-[#829889]">暂无日志文件</p>
                ) : contentLoading ? (
                    <p className="p-8 text-sm text-[#829889]">正在加载日志…</p>
                ) : contentFailed ? (
                    <p className="p-8 text-sm text-[#829889]">日志加载失败，请重试。</p>
                ) : content && content.log_lines.length === 0 ? (
                    <p className="p-8 text-sm text-[#829889]">最近 {content.lines} 行内无匹配</p>
                ) : (
                    <pre className="max-h-[70vh] overflow-auto p-4 font-mono text-xs leading-5 whitespace-pre text-[#d8f2e0]">
                        {content?.log_lines.join("\n")}
                    </pre>
                )}
            </div>
        </section>
    );
}
