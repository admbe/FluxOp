import { useEffect, useState } from "react";

/**
 * Global "something is generating" signal for the animated logo mark.
 * Same window-event pattern as the theme system: no store, no context —
 * pages announce, the logo listens. Sources are reference-counted so
 * overlapping fetches (three report sections in flight at once) keep the
 * mark animating until the last one settles.
 */
const activeSources = new Set<string>();

export function announceActivity(source: string, active: boolean): void {
  if (active) activeSources.add(source);
  else activeSources.delete(source);
  window.dispatchEvent(
    new CustomEvent("flux-activity", { detail: { active: activeSources.size > 0 } }),
  );
}

export function useGlobalActivity(): boolean {
  const [active, setActive] = useState(activeSources.size > 0);
  useEffect(() => {
    const onChange = (event: Event) =>
      setActive(Boolean((event as CustomEvent).detail?.active));
    window.addEventListener("flux-activity", onChange);
    return () => window.removeEventListener("flux-activity", onChange);
  }, []);
  return active;
}
