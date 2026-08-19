import type { ReactNode } from "react";
export default function UserLayout({ children }: { children: ReactNode }) { return <div className="h-dvh bg-background text-foreground">{children}</div>; }
