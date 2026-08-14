import type { ReactNode } from "react";

/** Configuration is provided by the authenticated same-origin service, never URL parameters. */
export function ClientRootInit({ children }: { children: ReactNode }) {
    return <>{children}</>;
}
