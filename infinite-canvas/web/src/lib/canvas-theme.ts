export type CanvasColorTheme = "light" | "dark";
export type CanvasBackgroundMode = "dots" | "lines" | "blank";

// 简约蓝白：与 Portal 色板同源（--page-bg #f3f6fa、--surface #fff、
// --border #d9e0ea、--text #172033、--accent #235fd6）。
export const canvasThemes = {
    light: {
        canvas: {
            background: "#f3f6fa",
            dot: "rgba(35,95,214,.22)",
            line: "rgba(35,95,214,.10)",
            selectionStroke: "#235fd6",
            selectionFill: "rgba(35,95,214,.08)",
        },
        node: {
            label: "#465267",
            fill: "#ffffff",
            panel: "#ffffff",
            stroke: "#d9e0ea",
            activeStroke: "#235fd6",
            placeholder: "#8b95a7",
            text: "#172033",
            muted: "#687386",
            faint: "#9aa4b4",
        },
        toolbar: {
            panel: "rgba(255,255,255,.96)",
            border: "#d9e0ea",
            item: "#465267",
            itemHover: "#eef2f7",
            activeBg: "#eef5ff",
            activeText: "#194fb8",
        },
    },
    dark: {
        canvas: {
            background: "#111a2b",
            dot: "rgba(216,228,248,.22)",
            line: "rgba(216,228,248,.10)",
            selectionStroke: "#5b8ff0",
            selectionFill: "rgba(91,143,240,.12)",
        },
        node: {
            label: "#c4d0e4",
            fill: "#f8fafc",
            panel: "#ffffff",
            stroke: "#c3ccd9",
            activeStroke: "#5b8ff0",
            placeholder: "#8b98b0",
            text: "#e8eefb",
            muted: "#c4d0e4",
            faint: "#7c8aa3",
        },
        toolbar: {
            panel: "rgba(26,35,56,.96)",
            border: "#c3ccd9",
            item: "#c4d0e4",
            itemHover: "#dbe7ff",
            activeBg: "#dbe7ff",
            activeText: "#e8eefb",
        },
    },
} as const;

export type CanvasTheme = (typeof canvasThemes)[CanvasColorTheme];
