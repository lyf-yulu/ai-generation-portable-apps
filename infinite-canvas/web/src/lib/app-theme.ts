import type { ThemeConfig } from "antd";
import { theme as antdTheme } from "antd";

// 蓝白主题，对齐 Portal 的 --accent #235fd6 / --accent-hover #194fb8
// （portal/static/styles.css:19-22）。
const neutral = {
    light: {
        primary: "#235fd6",
        primaryHover: "#194fb8",
        primaryText: "#ffffff",
        menuBg: "#eef5ff",
        menuText: "#172033",
        selectActiveBg: "#eef5ff",
        selectSelectedBg: "#dbe7ff",
        selectText: "#172033",
        tableSelectedBg: "rgba(35, 95, 214, 0.06)",
        tableSelectedHoverBg: "rgba(35, 95, 214, 0.10)",
    },
    dark: {
        primary: "#5b8ff0",
        primaryHover: "#7aa6f5",
        primaryText: "#0b1220",
        menuBg: "#eef5ff",
        menuText: "#e8eefb",
        selectActiveBg: "#eef5ff",
        selectSelectedBg: "#dbe7ff",
        selectText: "#e8eefb",
        tableSelectedBg: "rgba(91, 143, 240, 0.10)",
        tableSelectedHoverBg: "rgba(91, 143, 240, 0.16)",
    },
};

export function getAntThemeConfig(dark: boolean): ThemeConfig {
    const color = dark ? neutral.dark : neutral.light;

    return {
        algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        cssVar: { key: dark ? "infinite-canvas-dark" : "infinite-canvas-light" },
        token: {
            colorPrimary: color.primary,
            colorInfo: color.primary,
            colorLink: color.primary,
            colorLinkHover: color.primaryHover,
            colorLinkActive: color.primary,
            colorTextLightSolid: color.primaryText,
        },
        components: {
            Button: {
                primaryShadow: "none",
            },
            Menu: {
                itemActiveBg: color.menuBg,
                itemHoverBg: color.menuBg,
                itemSelectedBg: color.menuBg,
                itemSelectedColor: color.menuText,
                darkItemHoverBg: neutral.dark.menuBg,
                darkItemSelectedBg: neutral.dark.menuBg,
                darkItemSelectedColor: neutral.dark.menuText,
            },
            Select: {
                optionActiveBg: color.selectActiveBg,
                optionSelectedBg: color.selectSelectedBg,
                optionSelectedColor: color.selectText,
            },
            Table: {
                rowSelectedBg: color.tableSelectedBg,
                rowSelectedHoverBg: color.tableSelectedHoverBg,
            },
        },
    };
}
