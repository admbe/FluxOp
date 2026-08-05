import { AlertCircle, LoaderCircle, X } from "lucide-react";
import { useEffect } from "react";
import type { KeyboardEvent, ReactNode } from "react";

export function PageHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action && <div className="page-actions">{action}</div>}
    </header>
  );
}

export function Card({
  children,
  className = "",
  onClick,
}: {
  children: ReactNode;
  className?: string;
  onClick?: () => void;
}) {
  return (
    <section className={`card ${className}`} onClick={onClick}>
      {children}
    </section>
  );
}

/** Horizontal tab bar. Roving-tabindex plus arrow-key navigation so the
 *  group behaves as one control for keyboard and screen-reader users,
 *  which a row of plain buttons does not. */
export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
  label,
}: {
  tabs: { id: T; label: string; badge?: number }[];
  active: T;
  onChange: (id: T) => void;
  label: string;
}) {
  function onKeyDown(event: KeyboardEvent, index: number) {
    const delta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!delta) return;
    event.preventDefault();
    onChange(tabs[(index + delta + tabs.length) % tabs.length].id);
  }
  return (
    <div className="admin-tabs" role="tablist" aria-label={label}>
      {tabs.map((tab, index) => (
        <button
          key={tab.id}
          role="tab"
          id={`tab-${tab.id}`}
          aria-selected={tab.id === active}
          aria-controls={`tabpanel-${tab.id}`}
          tabIndex={tab.id === active ? 0 : -1}
          className={`admin-tab${tab.id === active ? " admin-tab--active" : ""}`}
          onClick={() => onChange(tab.id)}
          onKeyDown={(event) => onKeyDown(event, index)}
        >
          {tab.label}
          {tab.badge ? <span className="admin-tab__badge">{tab.badge}</span> : null}
        </button>
      ))}
    </div>
  );
}

export function Loading() {
  return (
    <div className="state-panel">
      <LoaderCircle className="spin" />
      <p>Loading workspace…</p>
    </div>
  );
}

export function ErrorPanel({ message }: { message: string }) {
  return (
    <div className="state-panel state-panel--error">
      <AlertCircle />
      <strong>Something went wrong</strong>
      <p>{message}</p>
    </div>
  );
}

/** Styled stand-in for window.confirm, so a destructive action gets the
 *  same modal chrome as everything else on the page instead of a bare
 *  browser dialog. Escape and backdrop click both cancel. */
export function ConfirmDialog({
  title,
  description,
  confirmLabel = "Confirm",
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  description: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);
  return (
    <div className="rz-overlay" role="alertdialog" aria-modal="true" aria-label={title} onClick={onCancel}>
      <div className="rz-modal rz-modal--confirm" onClick={(event) => event.stopPropagation()}>
        <div className="rz-modal__head">
          <h2>{title}</h2>
          <button className="icon-button" onClick={onCancel} aria-label="Cancel"><X size={16} /></button>
        </div>
        <p className="muted">{description}</p>
        <div className="rz-modal__actions">
          <button className="button button--secondary" onClick={onCancel} disabled={busy}>Cancel</button>
          <button
            className={danger ? "button button--danger" : "button"}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-orbit" />
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}
