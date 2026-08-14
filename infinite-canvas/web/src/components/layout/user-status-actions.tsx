import type { CSSProperties } from "react";
import { BookOpen, Keyboard } from "lucide-react";
import { AnimatedThemeToggler } from "@/components/ui/animated-theme-toggler";
import { DOCS_URL } from "@/constant/env";
import { useThemeStore } from "@/stores/use-theme-store";

export function UserStatusActions({ variant: _variant = "default", onOpenShortcuts }: { showConfig?: boolean; variant?: "default" | "canvas"; onOpenShortcuts?: () => void }) {
    const theme = useThemeStore((state) => state.theme);
    const setTheme = useThemeStore((state) => state.setTheme);
    const iconStyle: CSSProperties | undefined = undefined;
    const iconClass = "inline-flex size-7 shrink-0 items-center justify-center text-stone-600 transition hover:text-stone-950 dark:text-stone-300 dark:hover:text-white [&_svg]:size-4";
    return <div className="inline-flex shrink-0 items-center gap-1">
        <a href={DOCS_URL} target="_blank" rel="noopener noreferrer" className={iconClass} style={iconStyle} aria-label="文档"><BookOpen /></a>
        <AnimatedThemeToggler theme={theme} onThemeChange={setTheme} className={iconClass} aria-label="切换主题" title="切换主题" />
        {onOpenShortcuts ? <button type="button" className={iconClass} onClick={onOpenShortcuts} aria-label="快捷键"><Keyboard /></button> : null}
    </div>;
}
