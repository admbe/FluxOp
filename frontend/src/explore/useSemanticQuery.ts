import { useEffect, useState } from "react";
import { ApiError, api, isAbortError } from "../api";
import type { SemanticCatalog, SemanticQueryResult } from "../types";

type Status = "idle" | "loading" | "ok" | "error";

export type SemanticQueryState = {
  status: Status;
  result: SemanticQueryResult | null;
  error: string | null;
  /** HTTP status when the failure came from the API, else null. 503 is the
   *  bounded "snapshot not attached yet / database busy" path and deserves
   *  different copy from a 4xx contract error. */
  httpStatus: number | null;
};

export function useSemanticQuery(
  enabled: boolean,
  request: Parameters<typeof api.semanticQuery>[0] | null,
): SemanticQueryState {
  const [status, setStatus] = useState<Status>("idle");
  const [result, setResult] = useState<SemanticQueryResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [httpStatus, setHttpStatus] = useState<number | null>(null);

  useEffect(() => {
    if (!enabled || !request) {
      setStatus("idle");
      setResult(null);
      setError(null);
      setHttpStatus(null);
      return;
    }
    const ac = new AbortController();
    setStatus("loading");
    setError(null);
    setHttpStatus(null);
    // reconstruct request with signal — api.ts performRequest forwards init.signal to fetch
    api
      .semanticQuery(request, { signal: ac.signal })
      .then((r) => {
        if (ac.signal.aborted) return;
        setResult(r);
        setStatus("ok");
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted) return;
        if (isAbortError(e)) return;
        setError(e instanceof Error ? e.message : String(e));
        setHttpStatus(e instanceof ApiError ? e.status : null);
        setStatus("error");
      });
    return () => {
      ac.abort();
    };
  }, [enabled, JSON.stringify(request)]);

  return { status, result, error, httpStatus };
}

export type SemanticCatalogState = {
  status: Status;
  catalog: SemanticCatalog | null;
  error: string | null;
};

/**
 * The registry describes 60 dashboards; the catalog describes what the
 * semantic layer actually exposes. Fetching it once gives every catalog card
 * real measure/dimension counts, real availability and the real completeness
 * lag — without firing 60 queries at an analytical plane that opens and closes
 * a connection per query.
 */
export function useSemanticCatalog(enabled = true): SemanticCatalogState {
  const [status, setStatus] = useState<Status>("idle");
  const [catalog, setCatalog] = useState<SemanticCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) {
      setStatus("idle");
      return;
    }
    const ac = new AbortController();
    setStatus("loading");
    setError(null);
    api
      .semanticCatalog({ signal: ac.signal })
      .then((c) => {
        if (ac.signal.aborted) return;
        setCatalog(c);
        setStatus("ok");
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted) return;
        if (isAbortError(e)) return;
        setError(e instanceof Error ? e.message : String(e));
        setStatus("error");
      });
    return () => {
      ac.abort();
    };
  }, [enabled]);

  return { status, catalog, error };
}
