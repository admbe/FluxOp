import { useMemo, useState } from "react";

import type { Page } from "./types";

/** Parameters carried in the location hash after the page id: #/inventory?resourceType=... */
export function hashParams(): URLSearchParams {
  const hash = window.location.hash;
  const query = hash.includes("?") ? hash.slice(hash.indexOf("?") + 1) : "";
  return new URLSearchParams(query);
}

/** Navigate to a page carrying deep-link filter parameters. */
export function navigateWithParams(page: Page, params: Record<string, string>) {
  const value = new URLSearchParams();
  for (const [key, item] of Object.entries(params)) {
    if (item) value.set(key, item);
  }
  const suffix = value.toString();
  window.location.hash = `/${page}${suffix ? `?${suffix}` : ""}`;
}

export type SavedView = { name: string; params: Record<string, string> };

function storageKey(page: string) {
  return `flux-saved-views:${page}`;
}

export function loadSavedViews(page: string): SavedView[] {
  try {
    const raw = window.localStorage.getItem(storageKey(page));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function persistSavedViews(page: string, views: SavedView[]) {
  try {
    window.localStorage.setItem(storageKey(page), JSON.stringify(views));
  } catch {
    // Storage may be unavailable (private browsing); saved views degrade.
  }
}

/**
 * Saved filter presets for a page. `current` supplies the filters to persist
 * when the user saves; `onApply` receives a stored preset's filters.
 */
export function SavedViews({
  page,
  current,
  onApply,
}: {
  page: string;
  current: () => Record<string, string>;
  onApply: (params: Record<string, string>) => void;
}) {
  const [views, setViews] = useState<SavedView[]>(() => loadSavedViews(page));
  const [selected, setSelected] = useState("");
  const hasFilters = useMemo(
    () => Object.values(current()).some((value) => value),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [current],
  );

  function save() {
    const name = window.prompt("Name this view:");
    if (!name) return;
    const next = [
      ...views.filter((view) => view.name !== name),
      { name, params: current() },
    ].slice(-12);
    setViews(next);
    persistSavedViews(page, next);
    setSelected(name);
  }

  function apply(name: string) {
    setSelected(name);
    const view = views.find((item) => item.name === name);
    if (view) onApply(view.params);
  }

  function remove() {
    if (!selected) return;
    const next = views.filter((view) => view.name !== selected);
    setViews(next);
    persistSavedViews(page, next);
    setSelected("");
  }

  if (!views.length && !hasFilters) return null;
  return (
    <span className="saved-views">
      <select
        value={selected}
        onChange={(event) => apply(event.target.value)}
        aria-label="Saved views"
      >
        <option value="">Saved views…</option>
        {views.map((view) => (
          <option key={view.name} value={view.name}>{view.name}</option>
        ))}
      </select>
      <button type="button" className="button button--secondary" onClick={save}>
        Save view
      </button>
      {selected && (
        <button type="button" className="button button--secondary" onClick={remove}>
          Delete
        </button>
      )}
    </span>
  );
}
