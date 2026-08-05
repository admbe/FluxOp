import {
  Archive, ClipboardList, Clock, Copy, Cpu, Download, History, LayoutGrid,
  Maximize2, Minimize2, Pencil, Plus, Settings2, Sparkles, Star, Trash2,
  Upload, X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import { absoluteTime, currency, relativeTime } from "../format";
import type {
  PlanBucket, PlanLogEntry, PlanVm, RightsizingBoard,
  RightsizingImportPreview, RightsizingPlanBoard,
} from "../types";
import { Card, ConfirmDialog, ErrorPanel, Loading, PageHeader } from "../components/Ui";

const UNASSIGNED = "__unassigned__";
const NODATA = "__nodata__";

const SPECIAL_COLUMNS = [
  { key: UNASSIGNED, label: "Unassigned", cls: "rz-col--unassigned", hint: "Awaiting a plan decision" },
  { key: NODATA, label: "No monitoring data", cls: "rz-col--nodata", hint: "No telemetry evidence yet" },
  { key: "__review__", label: "Keep on demand", cls: "rz-col--unassigned", hint: "Resolve technical risk before commitment" },
  { key: "__savingsplan__", label: "Savings Plan candidates", cls: "rz-col--savings", hint: "Lifecycle-risk workloads; review commitment amount" },
  { key: "__excluded__", label: "Excluded", cls: "rz-col--excluded", hint: "Out of scope for this plan" },
];
const SPECIAL_KEYS = new Set(SPECIAL_COLUMNS.map((column) => column.key));
const DECISIONS = ["Pending", "Confirmed", "Needs discussion", "Deferred"];

// Recommendation "kind" values come from the governed evidence engine as
// machine keys (api/rightsizing.py); humanize them for badges and filters
// rather than showing e.g. the literal string "rightsizing_review".
const ACTION_LABELS: Record<string, string> = {
  resize: "Resize",
  shutdown: "Shutdown",
  rightsizing_review: "Needs review",
};
function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action.replaceAll("_", " ");
}

const ACTIVE_BOARD_STORAGE_KEY = "flux-rightsizing-active-board";

function columnLabel(key: string, buckets: PlanBucket[]): string {
  const special = SPECIAL_COLUMNS.find((column) => column.key === key);
  if (special) return special.label;
  const bucket = buckets.find((item) => item.bucketKey === key);
  return bucket ? `${bucket.sku} — ${bucket.region}` : key;
}

function strategyClass(strategy: string): string {
  const value = strategy.toLowerCase();
  if (value.includes("reservation")) return "rz-chip--ri";
  if (value.includes("savings")) return "rz-chip--sp";
  if (value.includes("keep")) return "rz-chip--keep";
  return "rz-chip--other";
}

type BucketFormState = {
  mode: "new" | "edit";
  bucketKey?: string;
  region: string;
  sku: string;
  strategy: string;
  refQuantity: string;
  refMonthlyPayg: string;
  refMonthlyRi1y: string;
  refRi1yUpfront: string;
  refMonthlySp1y: string;
  refMonthlySavings: string;
  refReservationCheck: string;
  note: string;
};

const BLANK_BUCKET_FORM: Omit<BucketFormState, "mode"> = {
  region: "", sku: "", strategy: "1-year reservation", refQuantity: "",
  refMonthlyPayg: "", refMonthlyRi1y: "", refRi1yUpfront: "",
  refMonthlySp1y: "", refMonthlySavings: "", refReservationCheck: "", note: "",
};

function bucketToForm(bucket: PlanBucket): BucketFormState {
  return {
    mode: "edit",
    bucketKey: bucket.bucketKey,
    region: bucket.region,
    sku: bucket.sku,
    strategy: bucket.strategy || "1-year reservation",
    refQuantity: bucket.refQuantity !== null ? String(bucket.refQuantity) : "",
    refMonthlyPayg: bucket.refMonthlyPayg !== null ? String(bucket.refMonthlyPayg) : "",
    refMonthlyRi1y: bucket.refMonthlyRi1y !== null ? String(bucket.refMonthlyRi1y) : "",
    refRi1yUpfront: bucket.refRi1yUpfront !== null ? String(bucket.refRi1yUpfront) : "",
    refMonthlySp1y: bucket.refMonthlySp1y !== null ? String(bucket.refMonthlySp1y) : "",
    refMonthlySavings: bucket.refMonthlySavings !== null ? String(bucket.refMonthlySavings) : "",
    refReservationCheck: bucket.refReservationCheck,
    note: bucket.note,
  };
}

function toNumberOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

type ImportTarget = "current" | "existing" | "new";

export function RightsizingPlanPage({
  canManage,
  onAskFlux,
}: {
  canManage: boolean;
  onAskFlux?: (question: string) => void;
}) {
  const [boards, setBoards] = useState<RightsizingBoard[] | null>(null);
  const [activeBoardId, setActiveBoardId] = useState(() => {
    try {
      return window.localStorage.getItem(ACTIVE_BOARD_STORAGE_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const [board, setBoard] = useState<RightsizingPlanBoard | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [tab, setTab] = useState<"board" | "log">("board");
  const [log, setLog] = useState<PlanLogEntry[] | null>(null);
  const [logLimit, setLogLimit] = useState(250);
  const [search, setSearch] = useState("");
  const [subscription, setSubscription] = useState("");
  const [action, setAction] = useState("");
  const [selected, setSelected] = useState<PlanVm | null>(null);
  const [modalTab, setModalTab] = useState<"details" | "history">("details");
  const [modalBucket, setModalBucket] = useState("");
  const [modalDecision, setModalDecision] = useState("Pending");
  const [modalNote, setModalNote] = useState("");
  const [savingModal, setSavingModal] = useState(false);
  const [bucketForm, setBucketForm] = useState<BucketFormState | null>(null);
  const [savingBucket, setSavingBucket] = useState(false);
  const [confirmDeleteBucket, setConfirmDeleteBucket] = useState<PlanBucket | null>(null);
  const [deletingBucket, setDeletingBucket] = useState(false);
  const [dragOver, setDragOver] = useState("");
  const [showImported, setShowImported] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const importInputRef = useRef<HTMLInputElement>(null);

  // Board management
  const [boardManagerOpen, setBoardManagerOpen] = useState(false);
  const [boardForm, setBoardForm] = useState<{ mode: "new" | "edit"; id?: string; name: string; description: string } | null>(null);
  const [boardBusy, setBoardBusy] = useState(false);
  const [confirmDeleteBoard, setConfirmDeleteBoard] = useState<RightsizingBoard | null>(null);

  // Import: file -> target picker -> dry-run preview -> apply
  const [importPayload, setImportPayload] = useState<Record<string, unknown> | null>(null);
  const [importFileName, setImportFileName] = useState("");
  const [importTarget, setImportTarget] = useState<ImportTarget>("current");
  const [importTargetBoardId, setImportTargetBoardId] = useState("");
  const [importNewBoardName, setImportNewBoardName] = useState("");
  const [importPreview, setImportPreview] = useState<RightsizingImportPreview | null>(null);
  const [importBusy, setImportBusy] = useState(false);

  useEffect(() => {
    if (!expanded) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", onKey);
    // The overlay owns scrolling while expanded; lock the page behind it.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [expanded]);

  const loadBoards = useCallback(() => {
    api.rightsizingBoards().then((value) => setBoards(value.boards)).catch(() => undefined);
  }, []);
  useEffect(loadBoards, [loadBoards]);

  const load = useCallback(() => {
    api.rightsizingPlan(activeBoardId)
      .then((value) => {
        setBoard(value);
        // A blank id resolves server-side to the primary board; adopt the
        // resolved id so the switcher and persisted choice reflect reality.
        if (value.boardId && value.boardId !== activeBoardId) {
          setActiveBoardId(value.boardId);
        }
      })
      .catch((reason) => setError(reason.message));
  }, [activeBoardId]);
  useEffect(load, [load]);

  useEffect(() => {
    try {
      if (activeBoardId) window.localStorage.setItem(ACTIVE_BOARD_STORAGE_KEY, activeBoardId);
    } catch {
      // Private browsing or storage disabled: the board still loads, it
      // just won't be remembered next visit.
    }
  }, [activeBoardId]);

  // A board switch invalidates any cached decision log and VM selection.
  function switchBoard(nextBoardId: string) {
    setActiveBoardId(nextBoardId);
    setBoard(null);
    setLog(null);
    setSelected(null);
    setTab("board");
  }

  function ensureLogLoaded(limit = logLimit) {
    api.rightsizingPlanLog(activeBoardId, limit)
      .then((value) => setLog(value.entries))
      .catch(() => setLog([]));
  }
  useEffect(() => {
    if (tab === "log" && log === null) ensureLogLoaded();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, log, activeBoardId]);

  function loadMoreLog() {
    const next = logLimit + 250;
    setLogLimit(next);
    ensureLogLoaded(next);
  }

  const vmHistory = useMemo(() => {
    if (!selected || !log) return [];
    return log.filter((entry) => entry.vmKey === selected.vmKey);
  }, [selected, log]);

  function openVmHistory() {
    setModalTab("history");
    if (log === null) ensureLogLoaded();
  }

  async function importPlanFile(file: File) {
    setError("");
    try {
      const payload = JSON.parse(await file.text()) as Record<string, unknown>;
      if (!payload || typeof payload !== "object" || !payload.assignments) {
        throw new Error(
          "This file does not look like a plan export: an 'assignments' section is required.",
        );
      }
      setImportPayload(payload);
      setImportFileName(file.name);
      setImportTarget("current");
      setImportTargetBoardId(activeBoardId);
      setImportNewBoardName(file.name.replace(/\.json$/i, "") || "Imported board");
      setImportPreview(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The plan file could not be read.");
    } finally {
      if (importInputRef.current) importInputRef.current.value = "";
    }
  }

  function closeImportFlow() {
    setImportPayload(null);
    setImportPreview(null);
    setImportBusy(false);
  }

  function importOptions(dryRun: boolean) {
    if (importTarget === "new") {
      return { newBoardName: importNewBoardName.trim(), dryRun };
    }
    if (importTarget === "existing") {
      return { boardId: importTargetBoardId, dryRun };
    }
    return { boardId: activeBoardId, dryRun };
  }

  function runImportPreview() {
    if (!importPayload) return;
    setImportBusy(true);
    setError("");
    api.importRightsizingPlan(importPayload, importOptions(true))
      .then((result) => setImportPreview(result as RightsizingImportPreview))
      .catch((reason) => setError(reason instanceof Error ? reason.message : "The plan could not be previewed."))
      .finally(() => setImportBusy(false));
  }

  function applyImport() {
    if (!importPayload) return;
    setImportBusy(true);
    setError("");
    api.importRightsizingPlan(importPayload, importOptions(false))
      .then((report) => {
        if (report.dryRun) return;
        let message =
          `Imported ${report.bucketsImported} bucket${report.bucketsImported === 1 ? "" : "s"} and `
          + `${report.assignmentsImported} placement${report.assignmentsImported === 1 ? "" : "s"} `
          + `(${report.matched} matched to live inventory, ${report.unmatched} preserved as historical), `
          + `plus ${report.logImported} log entries.`;
        if (report.matched === 0 && report.unmatched > 0) {
          const fileNames = (report.unmatchedSamples ?? []).join(", ");
          const inventoryNames = (report.inventorySample ?? []).join(", ");
          message +=
            ` No file names matched the ${report.inventoryVmCount ?? "known"} inventory VMs.`
            + (fileNames ? ` File examples: ${fileNames}.` : "")
            + (inventoryNames ? ` Inventory examples: ${inventoryNames}.` : "");
        }
        setNotice(message);
        closeImportFlow();
        loadBoards();
        if (report.boardId !== activeBoardId) {
          switchBoard(report.boardId);
        } else {
          setLog(null);
          load();
        }
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "The plan could not be imported."))
      .finally(() => setImportBusy(false));
  }

  function askFluxAboutVm(vm: PlanVm) {
    if (!onAskFlux || !board) return;
    const assignment = board.assignments[vm.vmKey];
    const column = columnLabel(effectiveBucket(vm), board.buckets);
    const decision = assignment?.decision || "Pending";
    const note = assignment?.note || "";
    setSelected(null);
    onAskFlux(
      `Perform a deep right-sizing review of the virtual machine "${vm.name}" `
      + `(resource ID ${vm.vmKey}, subscription ${vm.subscriptionName}). `
      + `It currently sits in the "${column}" column of the "${board.boardName}" purchase plan with `
      + `decision "${decision}"`
      + (note ? ` and note "${note}"` : "")
      + ". Retrieve its governed evidence dossier and assess: historical CPU, "
      + "memory, disk, and network utilization and their coverage windows; "
      + "visible usage patterns or seasonality in the trends; current SKU "
      + "versus the recommended target and their retail prices in this VM's "
      + "region; existing reservation or savings-plan coverage of its "
      + "charges and how a resize would interact with it; performance risk "
      + "of the change; estimated monthly and annual savings; and the "
      + "governed confidence with its factors. Conclude whether the plan "
      + "placement is right, wrong, or premature, citing the specific "
      + "evidence, and state what additional evidence would raise "
      + "confidence. Note anything the dossier cannot see, such as "
      + "application dependencies or resiliency requirements.",
    );
  }

  async function exportPlan() {
    if (!board) return;
    // Same envelope the importer accepts, so an export doubles as a backup
    // that can be re-imported (VMs are re-resolved by name on the way in).
    const assignments: Record<string, string> = {};
    const vmMeta: Record<string, { decision: string; note: string }> = {};
    for (const [vmKey, assignment] of Object.entries(board.assignments)) {
      assignments[vmKey] = assignment.bucketKey;
      if ((assignment.decision && assignment.decision !== "Pending") || assignment.note) {
        vmMeta[vmKey] = { decision: assignment.decision, note: assignment.note };
      }
    }
    const buckets: Record<string, unknown> = {};
    for (const bucket of board.buckets) {
      buckets[bucket.bucketKey] = {
        key: bucket.bucketKey,
        region: bucket.region,
        sku: bucket.sku,
        strategy: bucket.strategy,
        refQuantity: bucket.refQuantity,
        refMonthlyPaygBaseline: bucket.refMonthlyPayg,
        refMonthlyRi1YearCost: bucket.refMonthlyRi1y,
        refRi1YearUpfrontTotal: bucket.refRi1yUpfront,
        refMonthlySp1YearCost: bucket.refMonthlySp1y,
        refMonthlySavingsVsPayg: bucket.refMonthlySavings,
        refExistingReservationCheck: bucket.refReservationCheck,
        note: bucket.note,
      };
    }
    const vms = [
      ...board.vms.map((vm) => ({
        id: vm.vmKey,
        vmName: vm.name,
        subscriptionName: vm.subscriptionName,
        resourceGroup: vm.resourceGroup,
        region: vm.region,
      })),
      ...board.importedUnmatched.map((entry) => ({
        id: entry.vmKey,
        vmName: entry.vmName,
      })),
    ];
    try {
      const fullLog = (await api.rightsizingPlanLog(activeBoardId, 2000)).entries;
      const payload = {
        exportedAtUtc: new Date().toISOString(),
        boardName: board.boardName,
        buckets,
        assignments,
        vmMeta,
        log: fullLog.map((entry) => ({
          ts: entry.ts ? Date.parse(entry.ts) : null,
          vmId: entry.vmKey,
          vmName: entry.vmName,
          from: entry.fromLabel,
          to: entry.toLabel,
          decision: entry.decision,
          note: entry.note,
        })),
        vms,
      };
      const blob = new Blob([JSON.stringify(payload, null, 1)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      const boardSlug = board.boardName.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
      link.download = `flux-rightsizing-${boardSlug || "plan"}-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The plan could not be exported.");
    }
  }

  const effectiveBucket = useCallback(
    (vm: PlanVm): string => {
      const assigned = board?.assignments[vm.vmKey]?.bucketKey;
      if (assigned && assigned !== UNASSIGNED) return assigned;
      if (assigned === UNASSIGNED) return UNASSIGNED;
      return vm.noData ? NODATA : UNASSIGNED;
    },
    [board],
  );

  const subscriptions = useMemo(
    () => Array.from(new Set((board?.vms ?? []).map((vm) => vm.subscriptionName).filter(Boolean))).sort(),
    [board],
  );
  const actions = useMemo(
    () => Array.from(new Set((board?.vms ?? []).map((vm) => vm.action).filter((a) => a && a !== "none"))).sort(),
    [board],
  );

  const visible = useMemo(() => {
    if (!board) return [];
    const token = search.trim().toLowerCase();
    return board.vms.filter((vm) => {
      if (token && !`${vm.name} ${vm.sku} ${vm.region} ${vm.resourceGroup}`.toLowerCase().includes(token)) return false;
      if (subscription && vm.subscriptionName !== subscription) return false;
      if (action && vm.action !== action) return false;
      return true;
    });
  }, [board, search, subscription, action]);

  const byColumn = useMemo(() => {
    const map = new Map<string, PlanVm[]>();
    for (const vm of visible) {
      const key = effectiveBucket(vm);
      map.set(key, [...(map.get(key) ?? []), vm]);
    }
    return map;
  }, [visible, effectiveBucket]);

  // Derived live so the strip tracks optimistic moves instead of going
  // stale until the next full reload.
  const assignedCount = useMemo(
    () =>
      (board?.vms ?? []).filter((vm) => {
        const key = effectiveBucket(vm);
        return key !== UNASSIGNED && key !== NODATA;
      }).length,
    [board, effectiveBucket],
  );
  const activeBoard = boards?.find((item) => item.id === board?.boardId);
  const isFluxProposal = activeBoard?.createdBy === "flux";
  const canEdit = canManage && !isFluxProposal;

  function move(vm: PlanVm, bucketKey: string, decision?: string, note?: string) {
    if (!board || !canEdit) return;
    const previous = board.assignments[vm.vmKey];
    const optimistic = {
      ...board,
      assignments: {
        ...board.assignments,
        [vm.vmKey]: {
          bucketKey,
          decision: decision ?? previous?.decision ?? "Pending",
          note: note ?? previous?.note ?? "",
          refMonthlyPayg: previous?.refMonthlyPayg ?? null,
          refMonthlyCommitment: previous?.refMonthlyCommitment ?? null,
          refMonthlySavings: previous?.refMonthlySavings ?? null,
          economicsStatus: previous?.economicsStatus ?? "",
          updatedBy: "you",
          updatedAt: new Date().toISOString(),
        },
      },
    };
    setBoard(optimistic);
    setLog(null);
    api.moveRightsizingVms(board.boardId, [
      {
        vmKey: vm.vmKey,
        vmName: vm.name,
        subscriptionName: vm.subscriptionName,
        bucketKey,
        decision: decision ?? null,
        note: note ?? null,
      },
    ])
      .then(() => setNotice(`${vm.name} → ${columnLabel(bucketKey, board.buckets)}`))
      .catch((reason) => {
        setError(reason.message);
        load();
      });
  }

  function openVm(vm: PlanVm) {
    setSelected(vm);
    setModalTab("details");
    const assignment = board?.assignments[vm.vmKey];
    setModalBucket(assignment?.bucketKey ?? effectiveBucket(vm));
    setModalDecision(assignment?.decision || "Pending");
    setModalNote(assignment?.note ?? "");
  }

  function saveModal() {
    if (!selected) return;
    setSavingModal(true);
    move(selected, modalBucket, modalDecision, modalNote);
    setSelected(null);
    setSavingModal(false);
  }

  function saveBucketForm() {
    if (!bucketForm || !board) return;
    setSavingBucket(true);
    api.saveRightsizingBucket({
      boardId: board.boardId,
      region: bucketForm.region.trim(),
      sku: bucketForm.sku.trim(),
      strategy: bucketForm.strategy,
      refQuantity: bucketForm.refQuantity.trim() ? Math.round(Number(bucketForm.refQuantity)) : null,
      refMonthlyPayg: toNumberOrNull(bucketForm.refMonthlyPayg),
      refMonthlyRi1y: toNumberOrNull(bucketForm.refMonthlyRi1y),
      refRi1yUpfront: toNumberOrNull(bucketForm.refRi1yUpfront),
      refMonthlySp1y: toNumberOrNull(bucketForm.refMonthlySp1y),
      refMonthlySavings: toNumberOrNull(bucketForm.refMonthlySavings),
      refReservationCheck: bucketForm.refReservationCheck.trim(),
      note: bucketForm.note,
    })
      .then(() => {
        setBucketForm(null);
        load();
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setSavingBucket(false));
  }

  function requestDeleteBucket(bucket: PlanBucket) {
    setConfirmDeleteBucket(bucket);
  }

  function confirmBucketDeletion() {
    if (!confirmDeleteBucket) return;
    setDeletingBucket(true);
    api.deleteRightsizingBucket(confirmDeleteBucket.bucketKey)
      .then(() => {
        setConfirmDeleteBucket(null);
        setLog(null);
        load();
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setDeletingBucket(false));
  }

  // ---- Board management ----
  function saveBoardForm() {
    if (!boardForm) return;
    const name = boardForm.name.trim();
    if (!name) return;
    setBoardBusy(true);
    const request = boardForm.mode === "new"
      ? api.createRightsizingBoard(name, boardForm.description.trim())
      : api.renameRightsizingBoard(boardForm.id!, name, boardForm.description.trim());
    request
      .then((result) => {
        setBoardForm(null);
        loadBoards();
        if (boardForm.mode === "new" && "id" in result) {
          switchBoard(result.id);
        } else if (board && boardForm.id === board.boardId) {
          load();
        }
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setBoardBusy(false));
  }

  function setPrimaryBoard(target: RightsizingBoard) {
    setBoardBusy(true);
    api.setPrimaryRightsizingBoard(target.id)
      .then(() => loadBoards())
      .catch((reason) => setError(reason.message))
      .finally(() => setBoardBusy(false));
  }

  function copyProposal(target: RightsizingBoard) {
    const stamp = new Date().toISOString().slice(0, 10);
    setBoardBusy(true);
    api.duplicateRightsizingBoard(target.id, `Flux proposal copy — ${stamp}`)
      .then((copy) => {
        setBoardManagerOpen(false);
        setNotice("Editable copy created. Flux will continue refreshing only its proposal.");
        loadBoards();
        switchBoard(copy.id);
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setBoardBusy(false));
  }

  function confirmBoardDeletion() {
    if (!confirmDeleteBoard) return;
    setBoardBusy(true);
    api.deleteRightsizingBoard(confirmDeleteBoard.id)
      .then(() => {
        const wasActive = confirmDeleteBoard.id === activeBoardId;
        setConfirmDeleteBoard(null);
        loadBoards();
        if (wasActive) switchBoard("");
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setBoardBusy(false));
  }

  if (error && !board) return <ErrorPanel message={error} />;
  if (!board) return <Loading />;

  const columns: { key: string; bucket?: PlanBucket }[] = [
    ...SPECIAL_COLUMNS.map((column) => ({ key: column.key })),
    ...board.buckets.map((bucket) => ({ key: bucket.bucketKey, bucket })),
  ];

  return (
    <>
      <PageHeader
        eyebrow="FinOps"
        title="Right-sizing plan"
        description="Group virtual machines into commitment buckets by region and SKU, backed by governed telemetry evidence. Decisions are shared and logged."
        action={(
          <div className="rz-header-actions">
            {!canManage && (
              <span className="rz-readonly-hint">
                Read-only — the Flux.Admin role is required to edit the plan
              </span>
            )}
            {isFluxProposal && (
              <span className="rz-readonly-hint">
                Flux proposal — read-only and regenerated every 3 days
              </span>
            )}
            <button
              className="button button--secondary"
              onClick={() => void exportPlan()}
              title="Download the current plan as JSON (re-importable backup)"
            >
              <Download size={16} />Export
            </button>
            {canEdit && (
              <>
                <input
                  ref={importInputRef}
                  type="file"
                  accept="application/json,.json"
                  hidden
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) void importPlanFile(file);
                  }}
                />
                <button
                  className="button button--secondary"
                  onClick={() => importInputRef.current?.click()}
                  title="Import a plan export (JSON) from the standalone right-sizing tool"
                >
                  <Upload size={16} />Import plan
                </button>
                <button className="button" onClick={() => setBucketForm({ mode: "new", ...BLANK_BUCKET_FORM })}>
                  <Plus size={16} />New bucket
                </button>
              </>
            )}
            {canManage && activeBoard && isFluxProposal && (
              <button className="button" onClick={() => copyProposal(activeBoard)} disabled={boardBusy}>
                <Copy size={16} />{boardBusy ? "Copying…" : "Copy to editable board"}
              </button>
            )}
          </div>
        )}
      />
      {error && <div className="inline-alert inline-alert--error">{error}<button className="icon-button" onClick={() => setError("")} aria-label="Dismiss"><X size={13} /></button></div>}
      {notice && <div className="inline-alert">{notice}<button className="icon-button" onClick={() => setNotice("")} aria-label="Dismiss"><X size={13} /></button></div>}

      <div className="rz-board-switcher">
        <label className="select-field rz-board-select">
          <LayoutGrid size={14} />
          <select
            value={board.boardId}
            onChange={(event) => switchBoard(event.target.value)}
            aria-label="Active board"
          >
            {(boards ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}{item.isPrimary ? " (Primary)" : ""} · {item.bucketCount} bucket{item.bucketCount === 1 ? "" : "s"}
              </option>
            ))}
          </select>
        </label>
        {canManage && (
          <>
            <button className="button--ghost button" onClick={() => setBoardForm({ mode: "new", name: "", description: "" })}>
              <Plus size={13} />New board
            </button>
            <button className="button--ghost button" onClick={() => setBoardManagerOpen(true)}>
              <Settings2 size={13} />Manage boards
            </button>
          </>
        )}
        {board.boardId && boards?.find((b) => b.id === board.boardId)?.isPrimary && (
          <span className="rz-primary-pill" title="The fiscal outlook and AI evidence dossiers always track the primary board">
            <Star size={11} />Primary
          </span>
        )}
      </div>

      {board.boardDescription && (
        <details className="rz-method">
          <summary><Sparkles size={14} /><strong>Flux strategy and valuation method</strong><span>Why these placements and savings are defensible</span></summary>
          <p>{board.boardDescription}</p>
        </details>
      )}

      <div className="metrics-grid rz-summary">
        <Card className="metric-card"><span>Virtual machines</span><strong>{board.summary.totalVms.toLocaleString()}</strong><small>{board.summary.noData.toLocaleString()} without telemetry</small></Card>
        <Card className="metric-card"><span>Planned</span><strong>{assignedCount.toLocaleString()}</strong><small>{board.summary.totalVms ? Math.round((assignedCount / board.summary.totalVms) * 100) : 0}% of fleet placed</small></Card>
        <Card className="metric-card"><span>Buckets</span><strong>{board.summary.bucketCount.toLocaleString()}</strong><small>Region + SKU commitments</small></Card>
        <Card className="metric-card"><span>Modeled monthly savings</span><strong>{currency(board.summary.plannedMonthlySavings)}</strong><small>Retail-reconciled only · {board.summary.modeledReservationBuckets}/{board.summary.bucketCount} RI buckets · {board.summary.modeledSavingsPlanCandidates}/{board.summary.savingsPlanCandidates} SP candidates</small></Card>
      </div>

      <div className={expanded ? "rz-wrap rz-wrap--expanded" : "rz-wrap"}>
      <Card className="opportunity-filter-card rz-filters">
        <div className="filters filters--wrap">
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search name, SKU, region…" aria-label="Search virtual machines" />
          <label className="select-field"><select value={subscription} onChange={(event) => setSubscription(event.target.value)}><option value="">All subscriptions</option>{subscriptions.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <label className="select-field"><select value={action} onChange={(event) => setAction(event.target.value)}><option value="">All recommendations</option>{actions.map((value) => <option key={value} value={value}>{actionLabel(value)}</option>)}</select></label>
          <div className="rz-tabswitch" role="tablist" aria-label="Plan views">
            <button role="tab" aria-selected={tab === "board"} className={tab === "board" ? "active" : ""} onClick={() => setTab("board")}>Board</button>
            <button role="tab" aria-selected={tab === "log"} className={tab === "log" ? "active" : ""} onClick={() => setTab("log")}><ClipboardList size={13} /> Decision log</button>
          </div>
          {onAskFlux && (
            <button
              className="button button--secondary rz-askflux"
              onClick={() => onAskFlux(
                `Perform a deep review of the current right-sizing purchase plan ("${board.boardName}"). `
                + "Compare each bucket's member count against its "
                + "planned quantity; flag members whose governed telemetry, "
                + "recommendation, or commitment coverage disagrees with "
                + "their bucket's SKU or strategy; identify unassigned VMs "
                + "that appear to fit an existing bucket; and for the "
                + "highest-value or most contested placements, pull the "
                + "per-VM evidence dossier before judging. Weigh savings "
                + "against performance risk and evidence coverage, and "
                + "present everything as prioritized suggestions for the "
                + "planners with the supporting evidence and confidence.",
              )}
              title="Ask Flux Intelligence for a read-only review of the plan"
            >
              <Sparkles size={14} />Review with Flux
            </button>
          )}
          <button
            className="button button--secondary rz-expand"
            onClick={() => setExpanded((current) => !current)}
            title={expanded ? "Exit the full-width board (Esc)" : "Expand the board to the full window"}
          >
            {expanded ? <><Minimize2 size={14} />Exit full width</> : <><Maximize2 size={14} />Expand</>}
          </button>
        </div>
      </Card>

      {tab === "board" && (
        <div className="rz-board" role="list" aria-label="Plan board">
          {columns.map(({ key, bucket }) => {
            const special = SPECIAL_COLUMNS.find((column) => column.key === key);
            const members = byColumn.get(key) ?? [];
            const memberSavings = members.reduce((sum, vm) => sum + (vm.estimatedMonthlySaving ?? 0), 0);
            return (
              <section
                key={key}
                className={`rz-col ${special?.cls ?? ""} ${dragOver === key ? "rz-col--dragover" : ""}`}
                onDragOver={(event) => { if (canEdit) { event.preventDefault(); setDragOver(key); } }}
                onDragLeave={() => setDragOver((current) => (current === key ? "" : current))}
                onDrop={(event) => {
                  event.preventDefault();
                  setDragOver("");
                  const vmKey = event.dataTransfer.getData("text/plain");
                  const vm = board.vms.find((item) => item.vmKey === vmKey);
                  if (vm && effectiveBucket(vm) !== key) move(vm, key);
                }}
              >
                <header
                  className={`rz-col__head ${bucket && canEdit ? "rz-col__head--editable" : ""}`}
                  onClick={() => bucket && canEdit && setBucketForm(bucketToForm(bucket))}
                  title={bucket && canEdit ? "Click to edit this bucket" : undefined}
                >
                  <div className="rz-col__title">
                    <strong>{special ? special.label : `${bucket?.sku}`}</strong>
                    <span
                      className={`rz-count${bucket?.refQuantity && members.length > bucket.refQuantity ? " rz-count--over" : ""}`}
                      title={bucket?.refQuantity && members.length > bucket.refQuantity ? "More members than the planned commitment quantity" : undefined}
                    >
                      {members.length}{bucket?.refQuantity ? ` / ${bucket.refQuantity}` : ""}
                    </span>
                  </div>
                  {special && <small>{special.hint}</small>}
                  {bucket && (
                    <>
                      <small>
                        {bucket.region}
                        {bucket.source === "import" && <span className="rz-source-pill" title="Created by a plan import">imported</span>}
                      </small>
                      {bucket.strategy && <span className={`rz-chip ${strategyClass(bucket.strategy)}`}>{bucket.strategy}</span>}
                      <div className="rz-col__fin">
                        {bucket.refMonthlySavings !== null && <span>Saves {currency(bucket.refMonthlySavings)}/mo</span>}
                        {bucket.refMonthlyRi1y !== null && <span>RI {currency(bucket.refMonthlyRi1y)}/mo</span>}
                        {bucket.refRi1yUpfront !== null && <span>Upfront {currency(bucket.refRi1yUpfront)}</span>}
                        {memberSavings > 0 && <span title="Sum of governed per-VM saving estimates for members">Evidence {currency(memberSavings)}/mo</span>}
                      </div>
                      {bucket.note && <p className="rz-col__note" title={bucket.note}>{bucket.note}</p>}
                      {canEdit && (
                        <button
                          className="icon-button rz-col__remove"
                          onClick={(event) => { event.stopPropagation(); requestDeleteBucket(bucket); }}
                          aria-label={`Remove bucket ${bucket.sku} ${bucket.region}`}
                        >
                          <Trash2 size={13} />
                        </button>
                      )}
                    </>
                  )}
                </header>
                <div className="rz-col__body" role="listbox" aria-label={`${columnLabel(key, board.buckets)} members`}>
                  {members.map((vm) => (
                    <button
                      key={vm.vmKey}
                      className="rz-card"
                      draggable={canEdit}
                      onDragStart={(event) => event.dataTransfer.setData("text/plain", vm.vmKey)}
                      onClick={() => openVm(vm)}
                    >
                      <span className="rz-card__name">{vm.name}</span>
                      <span className="rz-card__meta">{vm.sku || "unknown SKU"} · {vm.region}</span>
                      <span className="rz-card__badges">
                        {vm.action !== "none" && <em className={`rz-badge rz-badge--${vm.action}`}>{actionLabel(vm.action)}</em>}
                        {vm.cpuP95 !== null && <em className="rz-badge rz-badge--cpu"><Cpu size={10} /> p95 {vm.cpuP95.toFixed(1)}%</em>}
                        {vm.noData && <em className="rz-badge rz-badge--nodata">no telemetry</em>}
                        {board.assignments[vm.vmKey]?.note && <em className="rz-badge rz-badge--note">note</em>}
                      </span>
                    </button>
                  ))}
                  {!members.length && <p className="rz-empty">Drop VMs here</p>}
                </div>
              </section>
            );
          })}
        </div>
      )}

      {tab === "log" && (
        <Card className="rz-log">
          <div className="section-heading"><div><h2>Decision log</h2><p>Every move, decision, and note on "{board.boardName}" — including entries imported from the standalone planning board.</p></div></div>
          {log === null && <p className="muted">Loading…</p>}
          {log !== null && !log.length && <p className="muted">No decisions logged yet.</p>}
          {log !== null && log.length > 0 && (
            <div className="rz-log__table">
              <div className="rz-log__row rz-log__row--head"><span>When</span><span>Who</span><span>VM</span><span>Move</span><span>Decision</span><span>Note</span></div>
              {log.map((entry, index) => (
                <div className="rz-log__row" key={index}>
                  <span title={entry.ts ? absoluteTime(entry.ts) : ""}>{entry.ts ? relativeTime(entry.ts) : "—"}</span>
                  <span>{entry.actor || "—"}</span>
                  <span>{entry.vmName || "—"}</span>
                  <span>{entry.fromLabel ? columnLabel(entry.fromLabel, board.buckets) : "—"} → {entry.toLabel ? columnLabel(entry.toLabel, board.buckets) : "—"}</span>
                  <span>{entry.decision || "—"}</span>
                  <span className="rz-log__note">{entry.note || ""}</span>
                </div>
              ))}
            </div>
          )}
          {log !== null && log.length >= logLimit && (
            <button className="button button--secondary rz-log__more" onClick={loadMoreLog}>Load more</button>
          )}
        </Card>
      )}
      </div>

      {board.importedUnmatched.length > 0 && (
        <Card className="rz-imported">
          <button className="rz-imported__toggle" onClick={() => setShowImported((value) => !value)}>
            <Archive size={14} />
            {board.importedUnmatched.length} imported decision{board.importedUnmatched.length === 1 ? "" : "s"} reference VMs no longer in inventory
            <span className="muted">{showImported ? "Hide" : "Show"}</span>
          </button>
          {showImported && (
            <div className="rz-imported__list">
              {board.importedUnmatched.map((item) => (
                <div key={item.vmKey}>
                  <strong>{item.vmName || item.vmKey}</strong>
                  <span>{columnLabel(item.bucketKey, board.buckets)} · {item.decision}{item.note ? ` · ${item.note}` : ""}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      {selected && (
        <div className="rz-overlay" role="dialog" aria-modal="true" aria-label={`${selected.name} details`} onClick={() => setSelected(null)}>
          <div className="rz-modal" onClick={(event) => event.stopPropagation()}>
            <div className="rz-modal__head">
              <div>
                <h2>{selected.name}</h2>
                <p className="muted">{selected.subscriptionName} · {selected.resourceGroup} · {selected.region}</p>
              </div>
              <button className="icon-button" onClick={() => setSelected(null)} aria-label="Close"><X size={16} /></button>
            </div>
            <div className="rz-tabswitch rz-modal__tabs" role="tablist" aria-label="VM detail views">
              <button role="tab" aria-selected={modalTab === "details"} className={modalTab === "details" ? "active" : ""} onClick={() => setModalTab("details")}>Details</button>
              <button role="tab" aria-selected={modalTab === "history"} className={modalTab === "history" ? "active" : ""} onClick={openVmHistory}><History size={12} />History</button>
            </div>
            {modalTab === "details" ? (
              <>
                <dl className="rz-modal__facts">
                  <dt>Current SKU</dt><dd>{selected.sku || "—"}</dd>
                  <dt>Monthly cost</dt><dd>{selected.estimatedMonthlyCost !== null ? currency(selected.estimatedMonthlyCost) : "—"}</dd>
                  <dt>CPU p95</dt><dd>{selected.cpuP95 !== null ? `${selected.cpuP95.toFixed(1)}% over ${selected.windowDays ?? "?"}d (${selected.coveragePercent?.toFixed(0) ?? "?"}% coverage)` : "No telemetry"}</dd>
                  <dt>Recommendation</dt><dd>{selected.action !== "none" ? `${actionLabel(selected.action)}${selected.targetSku ? ` → ${selected.targetSku}` : ""}` : "None"}</dd>
                  {selected.reason && (<><dt>Evidence</dt><dd>{selected.reason}</dd></>)}
                  {selected.estimatedMonthlySaving !== null && (<><dt>Est. saving</dt><dd>{currency(selected.estimatedMonthlySaving)}/mo</dd></>)}
                  {board.assignments[selected.vmKey]?.refMonthlyPayg !== null && board.assignments[selected.vmKey]?.refMonthlyPayg !== undefined && (<><dt>Validated baseline</dt><dd>{currency(board.assignments[selected.vmKey].refMonthlyPayg ?? 0)}/mo</dd></>)}
                  {board.assignments[selected.vmKey]?.refMonthlyCommitment !== null && board.assignments[selected.vmKey]?.refMonthlyCommitment !== undefined && (<><dt>Modeled commitment</dt><dd>{currency(board.assignments[selected.vmKey].refMonthlyCommitment ?? 0)}/mo</dd></>)}
                  {board.assignments[selected.vmKey]?.refMonthlySavings !== null && board.assignments[selected.vmKey]?.refMonthlySavings !== undefined && (<><dt>Modeled plan saving</dt><dd>{currency(board.assignments[selected.vmKey].refMonthlySavings ?? 0)}/mo</dd></>)}
                  {board.assignments[selected.vmKey]?.economicsStatus && (<><dt>Valuation status</dt><dd>{board.assignments[selected.vmKey].economicsStatus.replaceAll("-", " ")}</dd></>)}
                  {board.assignments[selected.vmKey]?.updatedAt && (
                    <><dt>Last noted</dt><dd>{board.assignments[selected.vmKey].updatedBy || "someone"}, {relativeTime(board.assignments[selected.vmKey].updatedAt)}</dd></>
                  )}
                </dl>
                {board.assignments[selected.vmKey]?.note && (
                  <div className="rz-rationale">
                    <h3>Flux decision rationale</h3>
                    <p>{board.assignments[selected.vmKey].note}</p>
                  </div>
                )}
                {canEdit ? (
                  <>
                    <label>Bucket
                      <select value={modalBucket} onChange={(event) => setModalBucket(event.target.value)}>
                        {SPECIAL_COLUMNS.map((column) => <option key={column.key} value={column.key}>{column.label}</option>)}
                        {board.buckets.map((bucket) => <option key={bucket.bucketKey} value={bucket.bucketKey}>{bucket.sku} — {bucket.region}</option>)}
                      </select>
                    </label>
                    <label>Decision
                      <select value={modalDecision} onChange={(event) => setModalDecision(event.target.value)}>
                        {DECISIONS.map((value) => <option key={value} value={value}>{value}</option>)}
                      </select>
                    </label>
                    <label>Note
                      <textarea value={modalNote} onChange={(event) => setModalNote(event.target.value)} maxLength={2000} placeholder="Why this placement?" />
                    </label>
                    <div className="rz-modal__actions">
                      {onAskFlux && (
                        <button
                          className="button button--secondary rz-modal__askflux"
                          onClick={() => { askFluxAboutVm(selected); }}
                          title="Ask Flux Intelligence to assess this VM and its placement"
                        >
                          <Sparkles size={14} />Ask Flux
                        </button>
                      )}
                      <button className="button button--secondary" onClick={() => setSelected(null)}>Cancel</button>
                      <button className="button" onClick={saveModal} disabled={savingModal}>{savingModal ? "Saving…" : "Save decision"}</button>
                    </div>
                  </>
                ) : (
                  <>
                    <p className="muted">
                      Placement: {columnLabel(effectiveBucket(selected), board.buckets)}
                      {board.assignments[selected.vmKey]?.note ? ` — ${board.assignments[selected.vmKey].note}` : ""}
                    </p>
                    {onAskFlux && (
                      <div className="rz-modal__actions">
                        <button
                          className="button button--secondary"
                          onClick={() => { askFluxAboutVm(selected); }}
                          title="Ask Flux Intelligence to assess this VM and its placement"
                        >
                          <Sparkles size={14} />Ask Flux
                        </button>
                      </div>
                    )}
                  </>
                )}
              </>
            ) : (
              <div className="rz-modal__history">
                {log === null && <p className="muted">Loading…</p>}
                {log !== null && vmHistory.length === 0 && <p className="muted">No decisions logged for this VM yet.</p>}
                {vmHistory.map((entry, index) => (
                  <div className="rz-history-entry" key={index}>
                    <span className="rz-history-entry__when"><Clock size={11} />{entry.ts ? absoluteTime(entry.ts) : "—"}</span>
                    <span>{entry.actor || "—"} moved it {entry.fromLabel ? columnLabel(entry.fromLabel, board.buckets) : "—"} → {entry.toLabel ? columnLabel(entry.toLabel, board.buckets) : "—"}{entry.decision ? `, marked ${entry.decision}` : ""}</span>
                    {entry.note && <p className="rz-history-entry__note">{entry.note}</p>}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {bucketForm && canEdit && (
        <div className="rz-overlay" role="dialog" aria-modal="true" aria-label={bucketForm.mode === "new" ? "New bucket" : "Edit bucket"} onClick={() => setBucketForm(null)}>
          <div className="rz-modal" onClick={(event) => event.stopPropagation()}>
            <div className="rz-modal__head">
              <div>
                <h2>{bucketForm.mode === "new" ? "New commitment bucket" : `${bucketForm.sku} — ${bucketForm.region}`}</h2>
                <p className="muted">{bucketForm.mode === "new" ? "One bucket per region and SKU pair." : "Region and SKU are fixed once a bucket exists."}</p>
              </div>
              <button className="icon-button" onClick={() => setBucketForm(null)} aria-label="Close"><X size={16} /></button>
            </div>
            {bucketForm.mode === "new" ? (
              <>
                <label>Region<input value={bucketForm.region} onChange={(event) => setBucketForm({ ...bucketForm, region: event.target.value })} placeholder="westus3" /></label>
                <label>SKU<input value={bucketForm.sku} onChange={(event) => setBucketForm({ ...bucketForm, sku: event.target.value })} placeholder="Standard_D4s_v5" /></label>
              </>
            ) : null}
            <label>Strategy
              <select value={bucketForm.strategy} onChange={(event) => setBucketForm({ ...bucketForm, strategy: event.target.value })}>
                <option>1-year reservation</option>
                <option>3-year reservation</option>
                <option>Savings plan</option>
                <option>Keep on demand</option>
              </select>
            </label>
            <div className="rz-form-grid">
              <label>Planned quantity<input value={bucketForm.refQuantity} onChange={(event) => setBucketForm({ ...bucketForm, refQuantity: event.target.value })} inputMode="numeric" placeholder="Optional" /></label>
              <label>Monthly savings<input value={bucketForm.refMonthlySavings} onChange={(event) => setBucketForm({ ...bucketForm, refMonthlySavings: event.target.value })} inputMode="decimal" placeholder="0.00" /></label>
              <label>Pay-as-you-go baseline<input value={bucketForm.refMonthlyPayg} onChange={(event) => setBucketForm({ ...bucketForm, refMonthlyPayg: event.target.value })} inputMode="decimal" placeholder="Optional" /></label>
              <label>1-year RI monthly<input value={bucketForm.refMonthlyRi1y} onChange={(event) => setBucketForm({ ...bucketForm, refMonthlyRi1y: event.target.value })} inputMode="decimal" placeholder="Optional" /></label>
              <label>1-year RI upfront<input value={bucketForm.refRi1yUpfront} onChange={(event) => setBucketForm({ ...bucketForm, refRi1yUpfront: event.target.value })} inputMode="decimal" placeholder="Optional" /></label>
              <label>1-year savings plan monthly<input value={bucketForm.refMonthlySp1y} onChange={(event) => setBucketForm({ ...bucketForm, refMonthlySp1y: event.target.value })} inputMode="decimal" placeholder="Optional" /></label>
            </div>
            <label>Existing reservation check<input value={bucketForm.refReservationCheck} onChange={(event) => setBucketForm({ ...bucketForm, refReservationCheck: event.target.value })} placeholder="Optional" /></label>
            <label>Note<textarea value={bucketForm.note} onChange={(event) => setBucketForm({ ...bucketForm, note: event.target.value })} maxLength={2000} placeholder="Why this bucket, this strategy?" /></label>
            <div className="rz-modal__actions">
              <button className="button button--secondary" onClick={() => setBucketForm(null)}>Cancel</button>
              <button
                className="button"
                onClick={saveBucketForm}
                disabled={savingBucket || !bucketForm.region.trim() || !bucketForm.sku.trim()}
              >
                {savingBucket ? "Saving…" : bucketForm.mode === "new" ? "Create bucket" : "Save changes"}
              </button>
            </div>
          </div>
        </div>
      )}

      {confirmDeleteBucket && (
        <ConfirmDialog
          title="Remove this bucket?"
          description={<>Removing <strong>{confirmDeleteBucket.sku} — {confirmDeleteBucket.region}</strong> returns its VMs to Unassigned and removes its {confirmDeleteBucket.refMonthlySavings ? `${currency(confirmDeleteBucket.refMonthlySavings)}/mo of ` : ""}planned savings from the fiscal outlook.</>}
          confirmLabel="Remove bucket"
          danger
          busy={deletingBucket}
          onConfirm={confirmBucketDeletion}
          onCancel={() => setConfirmDeleteBucket(null)}
        />
      )}

      {boardManagerOpen && (
        <div className="rz-overlay" role="dialog" aria-modal="true" aria-label="Manage boards" onClick={() => setBoardManagerOpen(false)}>
          <div className="rz-modal rz-modal--boards" onClick={(event) => event.stopPropagation()}>
            <div className="rz-modal__head">
              <div><h2>Manage boards</h2><p className="muted">Separate boards for separate plans — a migration wave, a team's proposal, a scratch exploration. Only the primary board counts toward the fiscal outlook.</p></div>
              <button className="icon-button" onClick={() => setBoardManagerOpen(false)} aria-label="Close"><X size={16} /></button>
            </div>
            <div className="rz-board-list">
              {(boards ?? []).map((item) => (
                <div className="rz-board-row" key={item.id}>
                  <div className="rz-board-row__name">
                    <strong>{item.name}</strong>
                    {item.isPrimary && <span className="rz-primary-pill"><Star size={11} />Primary</span>}
                    {item.createdBy === "flux" && <span className="rz-primary-pill"><Sparkles size={11} />System proposal</span>}
                    <small>{item.bucketCount} bucket{item.bucketCount === 1 ? "" : "s"} · {item.assignedCount} planned</small>
                    {item.description && <small className="muted">{item.description}</small>}
                  </div>
                  <div className="rz-board-row__actions">
                    {item.createdBy === "flux" ? (
                      <button className="button--ghost button" onClick={() => copyProposal(item)} disabled={boardBusy}>
                        <Copy size={13} />Copy
                      </button>
                    ) : !item.isPrimary && (
                      <button className="button--ghost button" onClick={() => setPrimaryBoard(item)} disabled={boardBusy} title="Make this the primary board">
                        <Star size={13} />Make primary
                      </button>
                    )}
                    {item.createdBy !== "flux" && (
                      <>
                        <button className="icon-button" onClick={() => setBoardForm({ mode: "edit", id: item.id, name: item.name, description: item.description })} aria-label={`Rename ${item.name}`}>
                          <Pencil size={14} />
                        </button>
                        <button
                          className="icon-button"
                          onClick={() => setConfirmDeleteBoard(item)}
                          disabled={item.isPrimary}
                          title={item.isPrimary ? "Set another board as primary before deleting this one" : "Delete this board"}
                          aria-label={`Delete ${item.name}`}
                        >
                          <Trash2 size={14} />
                        </button>
                      </>
                    )}
                  </div>
                </div>
              ))}
            </div>
            <div className="rz-modal__actions">
              <button className="button button--secondary" onClick={() => setBoardForm({ mode: "new", name: "", description: "" })}>
                <Plus size={14} />New board
              </button>
            </div>
          </div>
        </div>
      )}

      {boardForm && (
        <div className="rz-overlay" role="dialog" aria-modal="true" aria-label={boardForm.mode === "new" ? "New board" : "Rename board"} onClick={() => setBoardForm(null)}>
          <div className="rz-modal" onClick={(event) => event.stopPropagation()}>
            <div className="rz-modal__head">
              <h2>{boardForm.mode === "new" ? "New board" : "Rename board"}</h2>
              <button className="icon-button" onClick={() => setBoardForm(null)} aria-label="Close"><X size={16} /></button>
            </div>
            <label>Name<input value={boardForm.name} onChange={(event) => setBoardForm({ ...boardForm, name: event.target.value })} placeholder="Q3 migration candidates" autoFocus /></label>
            <label>Description (optional)<textarea value={boardForm.description} onChange={(event) => setBoardForm({ ...boardForm, description: event.target.value })} maxLength={500} placeholder="What is this board for?" /></label>
            <div className="rz-modal__actions">
              <button className="button button--secondary" onClick={() => setBoardForm(null)}>Cancel</button>
              <button className="button" onClick={saveBoardForm} disabled={boardBusy || !boardForm.name.trim()}>
                {boardBusy ? "Saving…" : boardForm.mode === "new" ? "Create board" : "Save name"}
              </button>
            </div>
          </div>
        </div>
      )}

      {confirmDeleteBoard && (
        <ConfirmDialog
          title="Delete this board?"
          description={<>Deleting <strong>{confirmDeleteBoard.name}</strong> permanently removes its {confirmDeleteBoard.bucketCount} bucket{confirmDeleteBoard.bucketCount === 1 ? "" : "s"} and every placement, decision, and note recorded on it.</>}
          confirmLabel="Delete board"
          danger
          busy={boardBusy}
          onConfirm={confirmBoardDeletion}
          onCancel={() => setConfirmDeleteBoard(null)}
        />
      )}

      {importPayload && (
        <div className="rz-overlay" role="dialog" aria-modal="true" aria-label="Import plan" onClick={closeImportFlow}>
          <div className="rz-modal rz-modal--import" onClick={(event) => event.stopPropagation()}>
            <div className="rz-modal__head">
              <div><h2>Import {importFileName}</h2><p className="muted">Preview what this file would change before applying it.</p></div>
              <button className="icon-button" onClick={closeImportFlow} aria-label="Close"><X size={16} /></button>
            </div>

            {!importPreview ? (
              <>
                <div className="rz-import-target" role="radiogroup" aria-label="Import target">
                  <label className="rz-import-target__option">
                    <input type="radio" checked={importTarget === "current"} onChange={() => setImportTarget("current")} />
                    Current board ({board.boardName})
                  </label>
                  <label className="rz-import-target__option">
                    <input type="radio" checked={importTarget === "existing"} onChange={() => setImportTarget("existing")} />
                    Another existing board
                    {importTarget === "existing" && (
                      <select value={importTargetBoardId} onChange={(event) => setImportTargetBoardId(event.target.value)}>
                        {(boards ?? []).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                      </select>
                    )}
                  </label>
                  <label className="rz-import-target__option">
                    <input type="radio" checked={importTarget === "new"} onChange={() => setImportTarget("new")} />
                    A new board
                    {importTarget === "new" && (
                      <input value={importNewBoardName} onChange={(event) => setImportNewBoardName(event.target.value)} placeholder="Board name" />
                    )}
                  </label>
                </div>
                <div className="rz-modal__actions">
                  <button className="button button--secondary" onClick={closeImportFlow}>Cancel</button>
                  <button
                    className="button"
                    onClick={runImportPreview}
                    disabled={importBusy || (importTarget === "new" && !importNewBoardName.trim()) || (importTarget === "existing" && !importTargetBoardId)}
                  >
                    {importBusy ? "Comparing…" : "Preview changes"}
                  </button>
                </div>
              </>
            ) : (
              <>
                <div className="rz-import-summary">
                  <span><strong>{importPreview.buckets.added.length}</strong> bucket{importPreview.buckets.added.length === 1 ? "" : "s"} added</span>
                  <span><strong>{importPreview.buckets.changed.length}</strong> changed</span>
                  <span><strong>{importPreview.buckets.unchanged}</strong> unchanged</span>
                  {importPreview.buckets.skipped > 0 && <span><strong>{importPreview.buckets.skipped}</strong> skipped</span>}
                </div>
                <div className="rz-import-summary">
                  <span><strong>{importPreview.assignments.added.length}</strong> placement{importPreview.assignments.added.length === 1 ? "" : "s"} added</span>
                  <span><strong>{importPreview.assignments.changed.length}</strong> changed</span>
                  <span><strong>{importPreview.assignments.unchanged}</strong> unchanged</span>
                </div>
                {importPreview.unmatched > 0 && (
                  <p className="inline-alert inline-alert--error">
                    {importPreview.unmatched} VM{importPreview.unmatched === 1 ? "" : "s"} in the file did not match live inventory by name and will be preserved as historical only.
                  </p>
                )}
                <div className="rz-import-diff">
                  {importPreview.buckets.added.map((entry) => (
                    <div className="rz-import-diff__row rz-import-diff__row--add" key={`bucket-add-${entry.region}-${entry.sku}`}>
                      <span className="rz-diff-tag rz-diff-tag--add">New bucket</span>{entry.label}
                    </div>
                  ))}
                  {importPreview.buckets.changed.map((entry) => (
                    <div className="rz-import-diff__row rz-import-diff__row--change" key={`bucket-change-${entry.region}-${entry.sku}`}>
                      <span className="rz-diff-tag rz-diff-tag--change">Bucket change</span>
                      {entry.label}: {entry.fields?.map((f) => `${f.field} ${f.before ?? "—"} → ${f.after ?? "—"}`).join(", ")}
                    </div>
                  ))}
                  {importPreview.assignments.added.map((entry) => (
                    <div className="rz-import-diff__row rz-import-diff__row--add" key={`assign-add-${entry.vmKey}`}>
                      <span className="rz-diff-tag rz-diff-tag--add">New placement</span>
                      {entry.vmName || entry.vmKey} → {entry.bucketLabel}
                      {!entry.resolved && <em className="rz-diff-note"> (not matched to live inventory)</em>}
                    </div>
                  ))}
                  {importPreview.assignments.changed.map((entry) => (
                    <div className="rz-import-diff__row rz-import-diff__row--change" key={`assign-change-${entry.vmKey}`}>
                      <span className="rz-diff-tag rz-diff-tag--change">Placement change</span>
                      {entry.vmName || entry.vmKey}: {entry.before.bucketLabel} → {entry.after.bucketLabel}
                      {entry.before.note !== entry.after.note && <em className="rz-diff-note"> · note updated</em>}
                      {entry.before.decision !== entry.after.decision && <em className="rz-diff-note"> · decision {entry.before.decision || "—"} → {entry.after.decision || "—"}</em>}
                    </div>
                  ))}
                  {!importPreview.buckets.added.length && !importPreview.buckets.changed.length
                    && !importPreview.assignments.added.length && !importPreview.assignments.changed.length && (
                    <p className="muted">This file matches the target board exactly — nothing would change.</p>
                  )}
                </div>
                <p className="muted rz-import-log-note">
                  {importPreview.logEntriesIncoming} imported log entr{importPreview.logEntriesIncoming === 1 ? "y" : "ies"} would replace {importPreview.logEntriesReplaced} currently on this board.
                </p>
                <div className="rz-modal__actions">
                  <button className="button button--secondary" onClick={() => setImportPreview(null)} disabled={importBusy}>Back</button>
                  <button className="button" onClick={applyImport} disabled={importBusy}>
                    {importBusy ? "Applying…" : "Apply import"}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
