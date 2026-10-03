import { useEffect, useState } from "react";
import { Tabs } from "../components/Ui";
import { CanvasPage } from "./CanvasPage";
import { SqlConsolePage } from "./SqlConsolePage";

type ExploreTab = "sql" | "canvas";

function tabFromHash(): ExploreTab {
  return /[?&]tab=canvas/.test(window.location.hash) ? "canvas" : "sql";
}

/**
 * The Explore surface. Two tabs only: the SQL console (with Ask Flux) is the
 * working instrument and lands first; the Command Center is the curated
 * read-out. The old 63-card catalog is gone — it duplicated what one good
 * console and one good canvas answer, and every card was another query
 * against the analytical plane.
 */
export function ExploreNativePage() {
  const [tab, setTab] = useState<ExploreTab>(tabFromHash);

  // Keep the tab in the hash so reloads and shared links land on the same
  // view: #/analytics?tab=canvas. A shared-query param (?q=<base64url SQL>)
  // survives the rewrite so the address stays copyable after load.
  useEffect(() => {
    const base = "#/analytics";
    const shared = window.location.hash.match(/[?&]q=([A-Za-z0-9_-]+)/)?.[1];
    const next =
      tab === "canvas"
        ? `${base}?tab=canvas`
        : shared
          ? `${base}?tab=sql&q=${shared}`
          : base;
    if (window.location.hash !== next) window.history.replaceState(null, "", next);
  }, [tab]);

  return (
    <div className="page explore-native">
      <Tabs
        label="Explore sections"
        active={tab}
        onChange={(id: ExploreTab) => setTab(id)}
        tabs={[
          { id: "sql", label: "SQL Console · Ask Flux" },
          { id: "canvas", label: "Command Center" },
        ]}
      />
      {tab === "sql" ? <SqlConsolePage /> : <CanvasPage />}
    </div>
  );
}
