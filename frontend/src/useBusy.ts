import { useSyncExternalStore } from "react";

import { isBusy, subscribeBusy } from "./busy";

// Kept separate from busy.ts so the counter stays importable from api.ts
// without pulling React into that module.
export function useBusy(): boolean {
  return useSyncExternalStore(subscribeBusy, isBusy, () => false);
}
