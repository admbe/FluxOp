import { useGlobalActivity } from "../busy";

/**
 * The Pulse mark: brand hexagon with a signal spike through it. While a
 * report or Flux Intelligence response is generating anywhere in the app,
 * the spike animates as a sweeping trace (CSS, reduced-motion aware).
 */
export function Logo({ compact = false }: { compact?: boolean }) {
  const busy = useGlobalActivity();
  return (
    <div className={`logo-lockup${busy ? " logo-lockup--busy" : ""}`} aria-label="Flux">
      <svg className="logo-mark" viewBox="0 0 24 24" aria-hidden="true">
        <path
          className="logo-hex"
          d="M12 2l9 5v10l-9 5-9-5V7z"
          fill="none"
          strokeWidth="1.7"
          strokeLinejoin="round"
        />
        <polyline
          className="logo-pulse"
          points="6.5,13 9.5,13 11.2,8 13.6,16.5 15.2,12 17.5,12"
          fill="none"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
          pathLength="100"
        />
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
