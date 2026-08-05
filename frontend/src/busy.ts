/**
 * Global "Flux is working" signal for the animated logo mark.
 *
 * Two producers feed one state, because they answer different questions:
 *
 *  - `trackBusy` wraps api.ts's single `request()` choke point, so every
 *    governed read counts automatically with nothing per-page to wire.
 *  - `announceActivity` is the named-source form pages already use for work
 *    that is not one fetch — a report tab firing five requests, or an Ask Flux
 *    turn that spans several tool calls. Sources are reference-counted by name
 *    so overlapping announcements keep the mark lit until the last one clears.
 *
 * `isBusy()` unions both, so the mark reflects either.
 *
 * Deliberately React-free: api.ts imports this, and pulling React into that
 * module's graph would be gratuitous. The hook lives in useBusy.ts.
 */

let depth = 0;
const activeSources = new Set<string>();
const listeners = new Set<() => void>();

export function isBusy(): boolean {
  return depth > 0 || activeSources.size > 0;
}

function emit(): void {
  for (const listener of listeners) listener();
  // Retained for any consumer still listening on the window event rather than
  // subscribing directly.
  window.dispatchEvent(
    new CustomEvent("flux-activity", { detail: { active: isBusy() } }),
  );
}

export function beginBusy(): void {
  const was = isBusy();
  depth += 1;
  if (!was) emit();
}

export function endBusy(): void {
  const was = isBusy();
  depth = Math.max(0, depth - 1);
  if (was !== isBusy()) emit();
}

export async function trackBusy<T>(work: () => Promise<T>): Promise<T> {
  beginBusy();
  try {
    return await work();
  } finally {
    endBusy();
  }
}

export function announceActivity(source: string, active: boolean): void {
  const was = isBusy();
  if (active) activeSources.add(source);
  else activeSources.delete(source);
  if (was !== isBusy()) emit();
}

export function subscribeBusy(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
