export function Logo({
  compact = false,
  busy = false,
}: {
  compact?: boolean;
  busy?: boolean;
}) {
  return (
    <div className="logo-lockup" aria-label="Flux">
      {/* Flux robot. Same geometry as the approved README banner and the
          marketing site's robot mark: solid head, dark visor panel, two lit
          eyes, ears, antenna.

          The visor MUST use the style property, not a fill= attribute —
          var() is not valid in SVG presentation attributes, so
          fill="rgb(var(--background))" silently inherits fill="none" from the
          root and the entire face disappears. currentColor IS valid as an
          attribute, which is why every other part uses fill="currentColor". */}
      <svg
        className={busy ? "logo-mark logo-mark--busy" : "logo-mark"}
        viewBox="0 0 200 200"
        fill="none"
        aria-hidden="true"
      >
        <circle
          className="fx-ring"
          cx="100"
          cy="20"
          fill="none"
          stroke="currentColor"
          strokeWidth="5"
        />
        <circle className="fx-dot" cx="100" cy="20" r="9" fill="currentColor" />
        <path d="M100 28 V48" stroke="currentColor" strokeWidth="9" strokeLinecap="round" />
        <rect x="18" y="84" width="16" height="38" rx="8" fill="currentColor" />
        <rect x="166" y="84" width="16" height="38" rx="8" fill="currentColor" />
        <rect x="38" y="46" width="124" height="108" rx="30" fill="currentColor" />
        <rect
          x="58"
          y="74"
          width="84"
          height="54"
          rx="24"
          style={{ fill: "rgb(var(--background))" }}
        />
        <circle className="fx-eye-l" cx="80" cy="101" r="9" fill="currentColor" />
        <circle className="fx-eye-r" cx="120" cy="101" r="9" fill="currentColor" />
      </svg>
      {!compact && (
        <div>
          <span className="logo-name">FLUX</span>
          <span className="logo-subtitle">Cloud intelligence</span>
        </div>
      )}
    </div>
  );
}
