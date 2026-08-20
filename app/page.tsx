"use client";

import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

type AccountState = "ready" | "running" | "cooldown" | "blocked" | "offline";
type RunState = "queued" | "running" | "succeeded" | "failed" | "cancelled";
type ConnectionState = "checking" | "live" | "partial" | "offline";

type Account = {
  id: string;
  owner: string;
  username: string;
  state: AccountState;
  activeRuns: number;
  maxParallel: number;
  gpuQuota: QuotaResource;
  tpuQuota: QuotaResource;
  quotaSyncedAt?: string;
  quotaRefreshAt?: string;
  quotaSyncError?: string;
  accelerator: string;
  lastSeen: string;
  controlState?: "enabled" | "disabled" | "revoked";
  credentialAvailable?: boolean;
  remoteReconciliationRequired?: boolean;
};

type QuotaResource = {
  usedHours: number | null;
  remainingHours: number | null;
  totalHours: number | null;
};

type Run = {
  id: string;
  name: string;
  accountId: string;
  owner: string;
  username: string;
  status: RunState;
  accelerator: string;
  acceleratorKind: string;
  machineShape?: string;
  runtimeInfo?: Record<string, string>;
  sourcePath: string;
  progress: number;
  createdAt: string;
  duration: string;
  metric?: string;
  outputDir?: string;
  resultSummary?: string;
  remoteMayBeRunning?: boolean;
  cancelSemantics?: string;
  logs: string[];
  logBeforeId?: number;
  hasOlderLogs?: boolean;
};

type AuditEvent = {
  id: string;
  actor: string;
  action: string;
  target: string;
  time: string;
  tone: "neutral" | "success" | "warning";
};

type DraftExperiment = {
  localId: string;
  name: string;
  accountId: string;
  sourcePath: string;
  kernelSlug: string;
  accelerator: string;
  machineShape: string;
};

type CredentialRefOption = {
  credential_env_ref: string;
  available: boolean;
  registered: boolean;
};

type SourceDirectory = {
  name: string;
  path: string;
  has_kernel_metadata: boolean;
};

type SourceListing = {
  root: string;
  current: string;
  parent: string | null;
  selectable: boolean;
  directories: SourceDirectory[];
};

type DesktopSettings = {
  desktop: boolean;
  source_root: string;
  credential_refs: string[];
  data_root: string;
  restart_required: boolean;
};

type DesktopResult = {
  ok: boolean;
  state?: "idle" | "pending" | "succeeded" | "failed";
  error?: string;
  cancelled?: boolean;
  credential_ref?: string;
  source_root?: string;
  restart_required?: boolean;
  username?: string;
};

type DesktopApi = {
  get_settings: () => Promise<DesktopSettings>;
  save_credential: (credentialRef: string, token: string) => Promise<DesktopResult>;
  start_kaggle_oauth: (credentialRef: string) => Promise<DesktopResult>;
  get_kaggle_oauth_status: () => Promise<DesktopResult>;
  forget_credential: (credentialRef: string) => Promise<DesktopResult>;
  choose_source_root: () => Promise<DesktopResult>;
  open_data_folder: () => Promise<DesktopResult>;
};

declare global {
  interface Window {
    pywebview?: { api: DesktopApi };
  }
}

const API_BASE = (
  process.env.NEXT_PUBLIC_CONTROL_PLANE_URL || "http://127.0.0.1:8765"
).replace(/\/$/, "");

function asList(value: unknown, keys: string[]): unknown[] {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    for (const key of keys) if (Array.isArray(record[key])) return record[key] as unknown[];
  }
  return [];
}

function textValue(record: Record<string, unknown>, keys: string[], fallback = "") {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" || typeof value === "number") return String(value);
  }
  return fallback;
}

function numberValue(record: Record<string, unknown>, keys: string[], fallback = 0) {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return fallback;
}

function objectValue(record: Record<string, unknown>, key: string) {
  const value = record[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function formatDuration(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remainingSeconds = whole % 60;
  return hours > 0
    ? `${hours}h ${String(minutes).padStart(2, "0")}m`
    : `${minutes}m ${String(remainingSeconds).padStart(2, "0")}s`;
}

function acceleratorLabel(accelerator: string, machineShape: string) {
  const kind = accelerator.toLowerCase();
  if (kind === "gpu") {
    const device = machineShape === "NvidiaTeslaT4" ? "Tesla T4" : machineShape;
    return device ? `GPU · ${device}` : "GPU";
  }
  if (kind === "tpu") {
    const device = machineShape === "TpuV38" ? "V3-8" : machineShape;
    return device ? `TPU · ${device}` : "TPU";
  }
  return kind === "cpu" ? "CPU" : accelerator || "Unknown";
}

function normalizeStatus(value: string): RunState {
  const status = value.toLowerCase();
  if (["complete", "completed", "success", "succeeded"].includes(status)) return "succeeded";
  if (["error", "failed", "failure"].includes(status)) return "failed";
  if (["cancelled", "canceled", "aborted"].includes(status)) return "cancelled";
  if (["pending", "submitted", "queued"].includes(status)) return "queued";
  return "running";
}

function normalizeAccount(value: unknown, index: number): Account {
  const raw = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
  const id = textValue(raw, ["id", "account_id", "accountId"], `account-${index + 1}`);
  const status = textValue(raw, ["state", "status"], raw.enabled === false ? "disabled" : "enabled").toLowerCase();
  const controlState = (["enabled", "disabled", "revoked"].includes(status) ? status : "enabled") as Account["controlState"];
  const activeRuns = numberValue(raw, ["active_runs", "activeRuns", "running_jobs"], 0);
  const credentialAvailable = raw.credential_available !== false;
  const remoteReconciliationRequired = raw.remote_reconciliation_required === true;
  const officialQuota = (raw.official_quota && typeof raw.official_quota === "object" ? raw.official_quota : {}) as Record<string, unknown>;
  const quotaResource = (name: "gpu" | "tpu"): QuotaResource => {
    const resource = (officialQuota[name] && typeof officialQuota[name] === "object" ? officialQuota[name] : {}) as Record<string, unknown>;
    const nullableNumber = (key: string) => typeof resource[key] === "number" ? resource[key] as number : null;
    return { usedHours: nullableNumber("used_hours"), remainingHours: nullableNumber("remaining_hours"), totalHours: nullableNumber("total_hours") };
  };
  return {
    id,
    owner: textValue(raw, ["owner", "owner_name", "display_name", "name"], `Member ${index + 1}`),
    username: textValue(raw, ["username", "kaggle_username", "handle"], id),
    state: controlState === "disabled" || controlState === "revoked" || !credentialAvailable ? "offline" : remoteReconciliationRequired ? "blocked" : activeRuns > 0 ? "running" : (["ready", "running", "cooldown", "blocked", "offline"].includes(status) ? status : "ready") as AccountState,
    activeRuns,
    maxParallel: numberValue(raw, ["max_parallel", "maxParallel", "concurrency"], 2),
    gpuQuota: quotaResource("gpu"),
    tpuQuota: quotaResource("tpu"),
    quotaSyncedAt: textValue(officialQuota, ["synced_at"], "") || undefined,
    quotaRefreshAt: textValue(officialQuota, ["refresh_at"], "") || undefined,
    quotaSyncError: textValue(officialQuota, ["sync_error"], "") || undefined,
    accelerator: textValue(raw, ["accelerator", "preferred_accelerator"], "Auto"),
    lastSeen: textValue(raw, ["last_seen", "lastSeen", "updated_at"], "unknown"),
    controlState,
    credentialAvailable,
    remoteReconciliationRequired,
  };
}

function quotaText(resource: QuotaResource) {
  return resource.remainingHours === null || resource.totalHours === null
    ? "Unavailable"
    : `${resource.remainingHours.toFixed(1)} / ${resource.totalHours.toFixed(1)}h`;
}

function eventLogLines(event: unknown): string[] {
  const item = (event && typeof event === "object" ? event : {}) as Record<string, unknown>;
  const timestamp = textValue(item, ["created_at", "time", "timestamp"], "");
  const shortTime = timestamp.includes("T") ? timestamp.split("T")[1]?.slice(0, 8) : timestamp;
  const level = textValue(item, ["level"], "info").toUpperCase();
  const message = textValue(item, ["message", "detail"], "Job event");
  const prefix = `${shortTime || "--:--:--"}`;
  const details = objectValue(item, "details");
  const remoteLines = Array.isArray(details.lines)
    ? details.lines.map((line) => `${prefix}  REMOTE  ${String(line)}`)
    : [];
  return [`${prefix}  ${level.padEnd(7)} ${message}`, ...remoteLines];
}

function normalizeRun(value: unknown, index: number, accounts: Account[]): Run {
  const raw = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
  const accountId = textValue(raw, ["account_id", "accountId", "owner_id"], "");
  const account = accounts.find((item) => item.id === accountId);
  const logValue = raw.logs ?? raw.log;
  const events = Array.isArray(raw.events) ? raw.events : [];
  const eventLogs = events.flatMap(eventLogLines);
  const logs = Array.isArray(logValue)
    ? logValue.map(String)
    : typeof logValue === "string"
      ? logValue.split("\n").filter(Boolean)
      : eventLogs.length
        ? eventLogs
        : ["Open this run while connected to load its event trace."];
  const resultValue = raw.result;
  const metadata = objectValue(raw, "metadata");
  const result = objectValue(raw, "result");
  const output = objectValue(result, "output");
  const runtimeSource = Object.keys(objectValue(raw, "runtime")).length
    ? objectValue(raw, "runtime")
    : objectValue(output, "runtime");
  const runtimeInfo = Object.fromEntries(
    Object.entries(runtimeSource)
      .filter(([, item]) => ["string", "number", "boolean"].includes(typeof item))
      .map(([key, item]) => [key, String(item)]),
  );
  const acceleratorKind = textValue(
    raw,
    ["accelerator"],
    textValue(metadata, ["accelerator"], "cpu"),
  );
  const machineShape = textValue(
    raw,
    ["machine_shape"],
    textValue(metadata, ["machine_shape"], ""),
  );
  const resultSummary = resultValue && typeof resultValue === "object"
    ? JSON.stringify(resultValue)
    : typeof resultValue === "string"
      ? resultValue
      : undefined;
  return {
    id: textValue(raw, ["id", "run_id", "runId"], `run-${index + 1}`),
    name: textValue(raw, ["name", "experiment_name", "title"], `Experiment ${index + 1}`),
    accountId,
    owner: textValue(raw, ["owner", "owner_name"], account?.owner || "Unassigned"),
    username: textValue(raw, ["username", "account_username"], account?.username || "unknown"),
    status: normalizeStatus(textValue(raw, ["status", "state"], "queued")),
    accelerator: acceleratorLabel(acceleratorKind, machineShape),
    acceleratorKind,
    machineShape: machineShape || undefined,
    runtimeInfo: Object.keys(runtimeInfo).length ? runtimeInfo : undefined,
    sourcePath: textValue(raw, ["source_dir", "source_path", "sourcePath", "kernel_ref", "kernel_slug"], "—"),
    progress: Math.min(100, Math.max(0, numberValue(raw, ["progress", "percent"], 0))),
    createdAt: textValue(raw, ["created_at", "createdAt", "started_at"], "—"),
    duration: textValue(
      raw,
      ["duration", "elapsed"],
      formatDuration(numberValue(raw, ["elapsed_seconds"], Number.NaN)),
    ),
    metric: textValue(raw, ["metric", "score"], "") || undefined,
    outputDir: textValue(raw, ["output_dir", "outputDir"], "") || undefined,
    resultSummary,
    remoteMayBeRunning: raw.remote_may_be_running === true,
    cancelSemantics: textValue(raw, ["cancel_semantics"], "") || undefined,
    logs,
    logBeforeId: events.reduce<number | undefined>((minimum, event) => {
      const item = (event && typeof event === "object" ? event : {}) as Record<string, unknown>;
      const sequence = numberValue(item, ["sequence_id"], 0);
      return sequence > 0 && (minimum === undefined || sequence < minimum) ? sequence : minimum;
    }, undefined),
    hasOlderLogs: events.length >= 200,
  };
}

function normalizeAudit(value: unknown, index: number): AuditEvent {
  const raw = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
  const action = textValue(raw, ["action", "event", "type"], "updated");
  const entityType = textValue(raw, ["entity_type", "target_type"], "workspace");
  const entityId = textValue(raw, ["entity_id", "target_id"], "");
  const details = raw.details && typeof raw.details === "object"
    ? raw.details as Record<string, unknown>
    : {};
  const detailValue = textValue(
    details,
    ["experiment_name", "kaggle_username", "reason", "status", "message"],
  );
  const changedFields = Array.isArray(details.fields)
    ? details.fields.map(String).join(", ")
    : "";
  const entityLabel = entityId ? `${entityType} · ${entityId}` : entityType;
  const detailLabel = detailValue || (changedFields ? `fields: ${changedFields}` : "");
  return {
    id: textValue(raw, ["id", "event_id"], `event-${index + 1}`),
    actor: textValue(raw, ["actor", "actor_name", "user"], "Control Plane"),
    action,
    target: textValue(raw, ["target", "resource", "detail"], detailLabel ? `${entityLabel} · ${detailLabel}` : entityLabel),
    time: textValue(raw, ["time", "created_at", "timestamp"], "now"),
    tone: action.includes("failed") || action.includes("revoked") ? "warning" : action.includes("completed") || action.includes("created") ? "success" : "neutral",
  };
}

async function apiRequest(path: string, init?: RequestInit, timeoutMs = 4500) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers || {}),
      },
    });
    const body = await response.text();
    let data: unknown = null;
    if (body) {
      try { data = JSON.parse(body); } catch { data = body; }
    }
    if (!response.ok) {
      const record = data && typeof data === "object" ? data as Record<string, unknown> : {};
      const errorRecord = record.error && typeof record.error === "object" ? record.error as Record<string, unknown> : {};
      throw new Error(textValue(errorRecord, ["message"], textValue(record, ["message"], `Request failed (${response.status})`)));
    }
    return data;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function downloadApiFile(path: string) {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    const body = await response.text();
    try {
      const parsed = JSON.parse(body) as Record<string, unknown>;
      const error = parsed.error && typeof parsed.error === "object" ? parsed.error as Record<string, unknown> : {};
      throw new Error(textValue(error, ["message"], `Download failed (${response.status})`));
    } catch (error) {
      if (error instanceof Error && !error.message.startsWith("Unexpected")) throw error;
      throw new Error(`Download failed (${response.status})`);
    }
  }
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || "download";
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function StatusBadge({ status }: { status: RunState }) {
  return (
    <span className={`status-badge status-${status}`}>
      <span className="status-dot" aria-hidden="true" />
      {status}
    </span>
  );
}

function AccountStateBadge({ state }: { state: AccountState }) {
  return (
    <span className={`account-state state-${state}`}>
      <span className="status-dot" aria-hidden="true" />
      {state}
    </span>
  );
}

function EmptyMark() {
  return <span aria-hidden="true">—</span>;
}

function ResultPreview({ value }: { value: string }) {
  const [expanded, setExpanded] = useState(false);
  const limit = expanded ? 20000 : 1500;
  const truncated = value.length > limit;
  return (
    <div className="result-preview">
      <code>{value.slice(0, limit)}{truncated ? "…" : ""}</code>
      {value.length > 1500 && <button type="button" onClick={() => setExpanded((current) => !current)}>{expanded ? "Collapse preview" : "Expand preview"}</button>}
      {expanded && value.length > 20000 && <small>Preview capped at 20,000 characters. Download the ZIP for the complete result.</small>}
    </div>
  );
}

export default function Home() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [refreshing, setRefreshing] = useState(false);
  const [composerOpen, setComposerOpen] = useState(false);
  const [accountFormOpen, setAccountFormOpen] = useState(false);
  const [desktopAvailable, setDesktopAvailable] = useState(false);
  const [desktopSettingsOpen, setDesktopSettingsOpen] = useState(false);
  const [managedAccount, setManagedAccount] = useState<Account | null>(null);
  const [selectedRun, setSelectedRun] = useState<Run | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | RunState>("all");

  const refreshRunDetail = useCallback(async (runId: string, quiet = false) => {
    if (connection !== "live") return;
    try {
      const data = await apiRequest(`/api/jobs/${encodeURIComponent(runId)}`);
      const record = data && typeof data === "object" ? data as Record<string, unknown> : {};
      const rawJob = record.job ?? data;
      const detailed = normalizeRun(rawJob, 0, accounts);
      setSelectedRun((current) => current?.id === runId
        ? { ...current, ...detailed, owner: current.owner, username: current.username }
        : current);
    } catch (error) {
      if (!quiet) setToast(`Could not load run detail: ${error instanceof Error ? error.message : "unknown error"}`);
    }
  }, [accounts, connection]);

  const openRun = async (run: Run) => {
    setSelectedRun(run);
    await refreshRunDetail(run.id);
  };

  const selectedRunId = selectedRun?.id;
  useEffect(() => {
    if (!selectedRunId || connection !== "live") return;
    const timer = window.setInterval(() => { void refreshRunDetail(selectedRunId, true); }, 10000);
    return () => window.clearInterval(timer);
  }, [selectedRunId, connection, refreshRunDetail]);

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    const results = await Promise.allSettled([
      apiRequest("/api/accounts"),
      apiRequest("/api/jobs"),
      apiRequest("/api/audit"),
    ]);

    const fulfilled = results.filter((item) => item.status === "fulfilled").length;
    const accountResult = results[0];
    let nextAccounts = accounts;
    if (accountResult.status === "fulfilled") {
      const list = asList(accountResult.value, ["accounts", "items", "data"]);
      nextAccounts = list.map(normalizeAccount);
    }
    const runResult = results[1];
    if (runResult.status === "fulfilled") {
      const list = asList(runResult.value, ["jobs", "runs", "items", "data"]);
      const nextRuns = list.map((item, index) => normalizeRun(item, index, nextAccounts));
      setRuns(nextRuns);
      nextAccounts = nextAccounts.map((account) => {
        const activeRuns = nextRuns.filter((run) => run.accountId === account.id && (run.status === "running" || run.status === "queued")).length;
        const state: AccountState = account.controlState !== "enabled" || account.credentialAvailable === false
          ? "offline"
          : account.remoteReconciliationRequired
            ? "blocked"
            : activeRuns > 0
              ? "running"
              : "ready";
        return { ...account, activeRuns, state };
      });
    }
    const auditResult = results[2];
    if (auditResult.status === "fulfilled") {
      const list = asList(auditResult.value, ["audit", "events", "audit_events", "items", "data"]);
      setAuditEvents(list.map(normalizeAudit));
    }

    if (accountResult.status === "fulfilled") setAccounts(nextAccounts);

    setConnection(fulfilled === 3 ? "live" : fulfilled > 0 ? "partial" : "offline");
    setRefreshing(false);
  }, [accounts]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void refresh(true); });
    // The first probe intentionally runs only once; later refreshes are explicit.
    return () => window.cancelAnimationFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const detectDesktop = () => setDesktopAvailable(Boolean(window.pywebview?.api));
    detectDesktop();
    window.addEventListener("pywebviewready", detectDesktop);
    return () => window.removeEventListener("pywebviewready", detectDesktop);
  }, []);

  useEffect(() => {
    if (connection !== "live") return;
    const timer = window.setInterval(() => { void refresh(true); }, 15000);
    return () => window.clearInterval(timer);
  }, [connection, refresh]);

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(null), 4200);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  useEffect(() => {
    if (!composerOpen && !selectedRun && !accountFormOpen && !managedAccount && !desktopSettingsOpen) return;
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        setComposerOpen(false);
        setSelectedRun(null);
        setAccountFormOpen(false);
        setManagedAccount(null);
        setDesktopSettingsOpen(false);
      }
    };
    document.addEventListener("keydown", closeOnEscape);
    document.body.classList.add("overlay-open");
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
      document.body.classList.remove("overlay-open");
    };
  }, [composerOpen, selectedRun, accountFormOpen, managedAccount, desktopSettingsOpen]);

  const readyCount = accounts.filter((item) => item.state === "ready" || item.state === "running").length;
  const activeCount = runs.filter((item) => item.status === "running" || item.status === "queued").length;
  const totalCapacity = accounts.reduce((sum, item) => sum + item.maxParallel, 0);
  const remainingGpuQuota = accounts.reduce((sum, item) => sum + (item.gpuQuota.remainingHours || 0), 0);
  const totalGpuQuota = accounts.reduce((sum, item) => sum + (item.gpuQuota.totalHours || 0), 0);
  const remainingTpuQuota = accounts.reduce((sum, item) => sum + (item.tpuQuota.remainingHours || 0), 0);
  const completedCount = runs.filter((run) => run.status === "succeeded").length;
  const scoredRun = runs.find((run) => run.metric);
  const filteredRuns = filter === "all" ? runs : runs.filter((run) => run.status === filter);

  const connectionLabel = connection === "live"
    ? "Control plane live"
    : connection === "partial"
      ? "Partial connection"
      : connection === "checking"
        ? "Checking API"
        : "API offline · no local preview data";

  const showOfflineNotice = connection === "offline";

  const scrollTo = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const runAction = async (run: Run, action: "cancel" | "retry" | "result") => {
    if (connection !== "live") {
      setToast(`API offline — ${action} was not sent for ${run.name}.`);
      return;
    }
    try {
      const data = await apiRequest(`/api/jobs/${encodeURIComponent(run.id)}/${action}`, {
        method: action === "result" ? "GET" : "POST",
      });
      if (action === "result" && data && typeof data === "object") {
        const record = data as Record<string, unknown>;
        const url = textValue(record, ["url", "download_url", "artifact_url"]);
        if (url) window.open(url, "_blank", "noopener,noreferrer");
        const rawJob = { ...run, ...record, logs: undefined, id: run.id, account_id: run.accountId, experiment_name: run.name };
        const resultRun = normalizeRun(rawJob, 0, accounts);
        setSelectedRun({ ...run, ...resultRun, owner: run.owner, username: run.username });
      }
      if (action === "cancel" && data && typeof data === "object") {
        const record = data as Record<string, unknown>;
        const rawJob = record.job && typeof record.job === "object"
          ? record.job as Record<string, unknown>
          : record;
        const semantics = textValue(record, ["cancel_semantics"], textValue(rawJob, ["cancel_semantics"], ""));
        const remoteMayBeRunning = rawJob.remote_may_be_running === true || record.remote_may_be_running === true;
        const cancelledRun = normalizeRun({ ...run, ...rawJob, logs: undefined, cancel_semantics: semantics }, 0, accounts);
        setSelectedRun({ ...run, ...cancelledRun, owner: run.owner, username: run.username });
        setToast(remoteMayBeRunning || semantics === "local_monitor_stop_requested"
          ? `Warning: local monitoring stopped for ${run.name}, but its Kaggle kernel may still be running. Reconcile the account before assigning new work.`
          : `${run.name}: queued job cancelled before remote execution.`);
      } else if (action === "result") {
        setToast(`${run.name}: result details loaded in this drawer.`);
      } else {
        setToast(`${run.name}: ${action} accepted by the control plane.`);
      }
      if (action !== "result") void refresh(true);
    } catch (error) {
      setToast(`${action} failed: ${error instanceof Error ? error.message : "unknown error"}`);
    }
  };

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to dashboard</a>

      <aside className="sidebar" aria-label="Primary navigation">
        <button className="brand" onClick={() => scrollTo("overview")} aria-label="Kaggle Team Control Plane home">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /><i /></span>
          <span className="brand-copy"><strong>KCP</strong><small>team control</small></span>
        </button>

        <nav className="side-nav">
          <button className="nav-item active" onClick={() => scrollTo("overview")}><span>01</span>Overview</button>
          <button className="nav-item" onClick={() => scrollTo("accounts")}><span>02</span>Accounts</button>
          <button className="nav-item" onClick={() => scrollTo("runs")}><span>03</span>Runs</button>
          <button className="nav-item" onClick={() => scrollTo("audit")}><span>04</span>Audit</button>
        </nav>

        <div className="sidebar-bottom">
          <div className={`connection-pill connection-${connection}`}>
            <span className="connection-pulse" aria-hidden="true" />
            <span><strong>{connectionLabel}</strong><small>{API_BASE}</small></span>
          </div>
          <div className="team-stack" aria-label={`${accounts.length} team accounts`}>
            {accounts.slice(0, 5).map((account) => <span key={account.id} title={account.owner}>{account.owner.slice(0, 1)}</span>)}
            {accounts.length > 5 && <span>+{accounts.length - 5}</span>}
          </div>
        </div>
      </aside>

      <main id="main-content" className="main-content">
        <header className="topbar">
          <div>
            <p className="eyebrow">Family ML workspace <span>/</span> v0.1</p>
            <h1>Experiment command center</h1>
          </div>
          <div className="topbar-actions">
            {desktopAvailable && (
              <button className="button button-quiet" onClick={() => setDesktopSettingsOpen(true)}>
                App settings
              </button>
            )}
            <button className="button button-quiet refresh-button" onClick={() => void refresh()} disabled={refreshing}>
              <span className={refreshing ? "spin" : ""} aria-hidden="true">↻</span>
              {refreshing ? "Syncing" : "Refresh"}
            </button>
            <button className="button button-primary" onClick={() => setComposerOpen(true)}>
              <span aria-hidden="true">＋</span> New batch
            </button>
          </div>
        </header>

        {showOfflineNotice && (
          <div className="offline-notice" role="status">
            <span className="offline-kicker">OFFLINE</span>
            <p><strong>No sample accounts are loaded.</strong> Start the real control-plane API at <code>{API_BASE}</code>, then refresh to connect owner accounts.</p>
            <button onClick={() => void refresh()}>Try connection</button>
          </div>
        )}

        <section id="overview" className="overview-section section-anchor" aria-labelledby="overview-title">
          <div className="hero-card">
            <div className="hero-grid" aria-hidden="true" />
            <div className="hero-copy">
              <p className="section-label">SYSTEM OVERVIEW</p>
              <h2 id="overview-title">{accounts.length ? `${accounts.length} connected account${accounts.length === 1 ? "" : "s"}.` : "Flexible account pool."}<br /><em>One clean queue.</em></h2>
              <p>Assign every experiment to a real owner, launch work in parallel, and watch the entire team from one place.</p>
              <button className="hero-cta" onClick={() => setComposerOpen(true)}>Compose experiment batch <span>→</span></button>
            </div>
            <div className="dispatch-visual" aria-label={`${readyCount} of ${accounts.length} accounts ready`}>
              <div className="dispatch-center">
                <span>READY</span>
                <strong>{readyCount}<small>/{accounts.length}</small></strong>
                <em>accounts</em>
              </div>
              {accounts.slice(0, 10).map((account, index) => (
                <div key={account.id} className={`orbit-node orbit-${index + 1} node-${account.state}`} title={`${account.owner}: ${account.state}`}>
                  {String(index + 1).padStart(2, "0")}
                </div>
              ))}
            </div>
          </div>

          <div className="stat-grid">
            <article className="stat-card stat-lime">
              <span className="stat-index">01</span>
              <p>Active runs</p>
              <strong>{activeCount}<small> / {totalCapacity}</small></strong>
              <div className="mini-bars" aria-hidden="true">{[42, 78, 58, 91, 66, 84, 50].map((height, i) => <i key={i} style={{ height: `${height}%` }} />)}</div>
            </article>
            <article className="stat-card">
              <span className="stat-index">02</span>
              <p>Official Kaggle GPU quota</p>
              <strong>{remainingGpuQuota.toFixed(0)}<small>h</small></strong>
              <div className="quota-track" role="progressbar" aria-label="Official Kaggle GPU quota remaining" aria-valuemin={0} aria-valuemax={totalGpuQuota} aria-valuenow={remainingGpuQuota}>
                <i style={{ width: `${totalGpuQuota ? remainingGpuQuota / totalGpuQuota * 100 : 0}%` }} />
              </div>
              <span className="stat-note">Kaggle API · TPU {remainingTpuQuota.toFixed(0)}h remaining</span>
            </article>
            <article className="stat-card">
              <span className="stat-index">03</span>
              <p>Completed runs</p>
              <strong>{completedCount}<small> jobs</small></strong>
              <span className="stat-note">Live control-plane history</span>
            </article>
            <article className="stat-card stat-dark">
              <span className="stat-index">04</span>
              <p>Latest reported score</p>
              <strong>{scoredRun?.metric || "—"}</strong>
              <span className="stat-note">{scoredRun ? `${scoredRun.name} · ${scoredRun.owner}` : "No metric reported yet"}</span>
            </article>
          </div>
        </section>

        <section id="accounts" className="section-block section-anchor" aria-labelledby="accounts-title">
          <div className="section-heading">
            <div>
              <p className="section-label">ACCOUNT FLEET</p>
              <h2 id="accounts-title">Every owner, visible.</h2>
            </div>
            <div className="account-heading-actions">
              <div className="legend" aria-label="Account status legend">
                <span><i className="legend-ready" />Ready</span><span><i className="legend-running" />Running</span><span><i className="legend-blocked" />Blocked</span><span><i className="legend-offline" />Offline</span>
              </div>
              <button className="button button-quiet" onClick={() => setAccountFormOpen(true)}>＋ Add member</button>
            </div>
          </div>

          <div className="account-grid">
            {accounts.length === 0 && <div className="empty-state"><strong>No accounts connected.</strong><span>Start the real API, then use Add member for each consenting owner.</span></div>}
            {accounts.map((account, index) => {
              const activeAccelerators = Array.from(new Set(
                runs
                  .filter((run) => run.accountId === account.id && (run.status === "running" || run.status === "queued"))
                  .map((run) => run.accelerator),
              ));
              const accountAccelerator = activeAccelerators.length
                ? activeAccelerators.join(" + ")
                : "Idle";
              return (
                <article className={`account-card account-${account.state}`} key={account.id}>
                  <div className="account-topline">
                    <span className="account-number">{String(index + 1).padStart(2, "0")}</span>
                    <AccountStateBadge state={account.state} />
                  </div>
                  <div className="account-person">
                    <span className="account-avatar" aria-hidden="true">{account.owner.slice(0, 1).toUpperCase()}</span>
                    <div><h3>{account.owner}</h3><p>@{account.username}</p></div>
                  </div>
                  {account.state === "blocked" && (
                    <div className="account-blocked-note" role="status">
                      <strong>Assignments blocked</strong>
                      <span>Remote run needs reconciliation</span>
                    </div>
                  )}
                  <div className="account-specs">
                    <span><small>ACTIVE ACCELERATOR</small><strong>{accountAccelerator}</strong></span>
                    <span><small>RUNS</small><strong>{account.activeRuns}/{account.maxParallel}</strong></span>
                  </div>
                  <div className="account-quota">
                    <div><span>GPU · Kaggle</span><strong>{quotaText(account.gpuQuota)}</strong></div>
                    <div><span>TPU · Kaggle</span><strong>{quotaText(account.tpuQuota)}</strong></div>
                    <small>{account.quotaSyncError ? "Sync unavailable — accelerator dispatch blocked" : account.quotaSyncedAt ? `Synced ${account.quotaSyncedAt}` : "Waiting for first Kaggle sync"}</small>
                  </div>
                  <footer><span><small>Last seen</small><strong>{account.lastSeen}</strong></span><button onClick={() => setManagedAccount(account)}>Manage</button></footer>
                </article>
              );
            })}
          </div>
        </section>

        <section id="runs" className="section-block section-anchor" aria-labelledby="runs-title">
          <div className="section-heading run-heading">
            <div>
              <p className="section-label">LIVE QUEUE</p>
              <h2 id="runs-title">Experiments in motion.</h2>
            </div>
            <div className="filter-tabs" role="group" aria-label="Filter runs">
              {(["all", "running", "queued", "succeeded", "failed"] as const).map((item) => (
                <button key={item} className={filter === item ? "active" : ""} aria-pressed={filter === item} onClick={() => setFilter(item)}>{item}</button>
              ))}
            </div>
          </div>

          <div className="run-table-wrap">
            <table className="run-table">
              <thead><tr><th scope="col">Experiment</th><th scope="col">Owner / account</th><th scope="col">Status</th><th scope="col">Progress</th><th scope="col">Runtime</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
              <tbody>
                {filteredRuns.map((run) => (
                  <tr key={run.id}>
                    <td><button className="run-name" onClick={() => void openRun(run)}><strong>{run.name}</strong><span>{run.sourcePath}</span></button></td>
                    <td><div className="run-owner"><span>{run.owner.slice(0, 1)}</span><div><strong>{run.owner}</strong><small>@{run.username}</small></div></div></td>
                    <td><StatusBadge status={run.status} /></td>
                    <td>
                      <div className="run-progress"><div><i style={{ width: `${run.progress}%` }} /></div><span>{run.progress}%</span></div>
                    </td>
                    <td><strong className="runtime">{run.duration}</strong><small className="accelerator">{run.accelerator}</small></td>
                    <td><button className="row-action" onClick={() => void openRun(run)} aria-label={`Open ${run.name} details`}>Open <span>↗</span></button></td>
                  </tr>
                ))}
                {!filteredRuns.length && <tr><td colSpan={6} className="empty-state">No runs match this filter.</td></tr>}
              </tbody>
            </table>
          </div>
        </section>

        <section id="audit" className="section-block split-section section-anchor" aria-labelledby="audit-title">
          <div className="audit-panel">
            <div className="panel-heading"><div><p className="section-label">ACTIVITY LOG</p><h2 id="audit-title">Control-plane events.</h2><p className="audit-disclaimer">Actor labels show the server-observed client mode, not a verified individual identity.</p></div><span className="live-tag">LIVE</span></div>
            <ol className="audit-list">
              {auditEvents.slice(0, 7).map((event) => (
                <li key={event.id}>
                  <span className={`audit-mark audit-${event.tone}`}>{event.actor.slice(0, 1).toUpperCase()}</span>
                  <p><strong>Actor label: {event.actor}</strong><br /><span>{event.action} · {event.target}</span></p>
                  <time>{event.time}</time>
                </li>
              ))}
              {!auditEvents.length && <li className="empty-audit">No audit events yet.</li>}
            </ol>
          </div>

          <div className="agent-panel">
            <span className="agent-kicker">AGENT READY</span>
            <h2>Give your AI<br />one control surface.</h2>
            <p>The same account registry, batch queue, logs, and artifacts are exposed through the local API.</p>
            <div className="endpoint-block">
              <span>CONTROL PLANE</span>
              <code>{API_BASE}</code>
              <button onClick={() => {
                void navigator.clipboard?.writeText(API_BASE);
                setToast("Control-plane URL copied.");
              }}>Copy</button>
            </div>
            <div className="agent-command"><span>agent</span><code>submit batch experiment-batch</code><i aria-hidden="true" /></div>
          </div>
        </section>

        <footer className="page-footer"><p>Kaggle Team Control Plane <span>·</span> MVP round 01</p><p>Credentials stay behind the local control plane.</p></footer>
      </main>

      {composerOpen && (
        <BatchComposer
          accounts={accounts}
          connection={connection}
          onClose={() => setComposerOpen(false)}
          onCreated={(message) => {
            setToast(message);
            setComposerOpen(false);
            if (connection === "live") void refresh(true);
          }}
        />
      )}

      {selectedRun && (
        <RunDrawer
          key={selectedRun.id}
          run={selectedRun}
          connection={connection}
          onClose={() => setSelectedRun(null)}
          onAction={runAction}
          onRefresh={() => void refreshRunDetail(selectedRun.id)}
        />
      )}

      {(accountFormOpen || managedAccount) && (
        <AccountDialog
          account={managedAccount}
          connection={connection}
          onClose={() => { setAccountFormOpen(false); setManagedAccount(null); }}
          onFinished={(message) => {
            setToast(message);
            setAccountFormOpen(false);
            setManagedAccount(null);
            if (connection === "live") void refresh(true);
          }}
        />
      )}

      {desktopSettingsOpen && (
        <DesktopSettingsDialog
          onClose={() => setDesktopSettingsOpen(false)}
          onChanged={(message) => {
            setToast(message);
            if (connection === "live") void refresh(true);
          }}
        />
      )}

      {toast && <div className="toast" role="status"><span aria-hidden="true">●</span>{toast}<button onClick={() => setToast(null)} aria-label="Dismiss notification">×</button></div>}
    </div>
  );
}

function DesktopSettingsDialog({ onClose, onChanged }: {
  onClose: () => void;
  onChanged: (message: string) => void;
}) {
  const [settings, setSettings] = useState<DesktopSettings | null>(null);
  const [credentialRef, setCredentialRef] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [oauthBusy, setOauthBusy] = useState(false);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    const api = window.pywebview?.api;
    if (!api) return;
    const next = await api.get_settings();
    setSettings(next);
    setCredentialRef((current) => {
      if (current) return current;
      const used = new Set(next.credential_refs);
      let index = 1;
      while (used.has(`KCP_KAGGLE_MEMBER_${String(index).padStart(2, "0")}`)) index += 1;
      return `KCP_KAGGLE_MEMBER_${String(index).padStart(2, "0")}`;
    });
  }, []);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void reload(); });
    return () => window.cancelAnimationFrame(frame);
  }, [reload]);

  const saveToken = async (event: FormEvent) => {
    event.preventDefault();
    const api = window.pywebview?.api;
    if (!api || !credentialRef.trim() || !token.trim()) return;
    setBusy(true);
    setError("");
    const result = await api.save_credential(credentialRef.trim(), token);
    setBusy(false);
    if (!result.ok) {
      setError(result.error || "Could not save this token.");
      return;
    }
    setToken("");
    onChanged(`${result.credential_ref} saved with Windows encryption. No rebuild needed.`);
    await reload();
  };

  const signInWithKaggle = async () => {
    const api = window.pywebview?.api;
    if (!api || !credentialRef.trim() || oauthBusy) return;
    setOauthBusy(true);
    setError("");
    const started = await api.start_kaggle_oauth(credentialRef.trim());
    if (!started.ok) {
      setOauthBusy(false);
      setError(started.error || "Could not start Kaggle sign-in.");
      return;
    }
    for (let attempt = 0; attempt < 300; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      const status = await api.get_kaggle_oauth_status();
      if (status.state === "pending") continue;
      setOauthBusy(false);
      if (status.state === "succeeded") {
        await reload();
        onChanged(`${status.credential_ref || credentialRef} connected as @${status.username || "Kaggle member"}.`);
      } else {
        setError(status.error || "Kaggle sign-in did not complete.");
      }
      return;
    }
    setOauthBusy(false);
    setError("Kaggle sign-in timed out. You can start it again.");
  };

  const forgetToken = async (ref: string) => {
    if (!window.confirm(`Forget the encrypted token ${ref}? Its account will become unavailable.`)) return;
    const result = await window.pywebview?.api.forget_credential(ref);
    if (!result?.ok) {
      setError(result?.error || "Could not forget this token.");
      return;
    }
    onChanged(`${ref} removed. Registered account history was kept.`);
    await reload();
  };

  const chooseSource = async () => {
    const result = await window.pywebview?.api.choose_source_root();
    if (!result?.ok) {
      if (!result?.cancelled) setError(result?.error || "Could not select this folder.");
      return;
    }
    setSettings((current) => current ? { ...current, source_root: result.source_root || current.source_root, restart_required: true } : current);
    onChanged("Source folder saved. Restart the app once to activate it.");
  };

  return (
    <div className="modal-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="account-dialog desktop-settings" role="dialog" aria-modal="true" aria-labelledby="desktop-settings-title">
        <header className="composer-header">
          <div><p className="section-label">WINDOWS APP</p><h2 id="desktop-settings-title">Local app settings.</h2><p>Tokens and accounts are runtime data. Changing them never rebuilds the app.</p></div>
          <button className="close-button" type="button" onClick={onClose} aria-label="Close app settings">×</button>
        </header>
        <div className="desktop-settings-body">
          {error && <p className="form-error" role="alert">{error}</p>}
          <section className="settings-section">
            <div><p className="section-label">ENCRYPTED TOKENS</p><h3>Owner credentials</h3></div>
            <div className="oauth-onboarding">
              <div><strong>Sign in with Kaggle</strong><span>Password and 2FA stay in Kaggle&apos;s browser. OAuth credentials are encrypted with Windows DPAPI.</span></div>
              <button className="button button-primary" type="button" disabled={oauthBusy || !credentialRef.trim()} onClick={() => void signInWithKaggle()}>{oauthBusy ? "Waiting for Kaggle…" : "Sign in with Kaggle"}</button>
            </div>
            <div className="credential-list">
              {settings?.credential_refs.map((ref) => (
                <div key={ref}><code>{ref}</code><button type="button" onClick={() => { setCredentialRef(ref); setToken(""); }}>Replace</button><button type="button" className="danger-link" onClick={() => void forgetToken(ref)}>Forget</button></div>
              ))}
              {settings && settings.credential_refs.length === 0 && <p>No encrypted tokens saved yet.</p>}
            </div>
            <form className="desktop-token-form" onSubmit={saveToken}>
              <label><span>Credential name</span><input value={credentialRef} onChange={(event) => setCredentialRef(event.target.value.toUpperCase())} placeholder="KCP_KAGGLE_MEMBER_01" /></label>
              <label><span>Kaggle API token</span><input type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} placeholder="Paste token to add or replace" /></label>
              <button className="button button-primary" type="submit" disabled={busy || !token.trim()}>{busy ? "Encrypting…" : "Save token"}</button>
            </form>
          </section>
          <section className="settings-section">
            <div><p className="section-label">EXPERIMENT FILES</p><h3>Source folder</h3></div>
            <code className="settings-path">{settings?.source_root || "Loading…"}</code>
            <div className="settings-actions"><button className="button button-quiet" type="button" onClick={() => void chooseSource()}>Browse folder</button><button className="button button-quiet" type="button" onClick={() => void window.pywebview?.api.open_data_folder()}>Open app data</button></div>
            {settings?.restart_required && <p className="restart-note">Restart the app to activate the new source folder. Token changes are already active.</p>}
          </section>
        </div>
      </section>
    </div>
  );
}

function AccountDialog({ account, connection, onClose, onFinished }: {
  account: Account | null;
  connection: ConnectionState;
  onClose: () => void;
  onFinished: (message: string) => void;
}) {
  const [ownerName, setOwnerName] = useState("");
  const [username, setUsername] = useState("");
  const [credentialEnvRef, setCredentialEnvRef] = useState("");
  const [consentBy, setConsentBy] = useState("");
  const [consentNote, setConsentNote] = useState("Shared family ML workspace access");
  const [consentChecked, setConsentChecked] = useState(false);
  const [revokeText, setRevokeText] = useState("");
  const [reconcileText, setReconcileText] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [credentialOptions, setCredentialOptions] = useState<CredentialRefOption[]>([]);
  const [inspectingCredential, setInspectingCredential] = useState(false);
  const [credentialVerified, setCredentialVerified] = useState(false);
  const [oauthBusy, setOauthBusy] = useState(false);

  const isOffline = connection !== "live";
  const controlState = account?.controlState || (account?.state === "offline" ? "disabled" : "enabled");

  useEffect(() => {
    if (account || isOffline) return;
    void apiRequest("/api/credentials")
      .then((value) => {
        const options = asList(value, ["credentials"]).map((item) => {
          const raw = (item && typeof item === "object" ? item : {}) as Record<string, unknown>;
          return {
            credential_env_ref: textValue(raw, ["credential_env_ref"]),
            available: raw.available === true,
            registered: raw.registered === true,
          };
        }).filter((item) => item.credential_env_ref);
        setCredentialOptions(options);
        const firstUnused = options.find((item) => item.available && !item.registered);
        if (firstUnused) setCredentialEnvRef(firstUnused.credential_env_ref);
      })
      .catch(() => undefined);
  }, [account, isOffline]);

  const signInWithKaggle = async () => {
    const api = window.pywebview?.api;
    if (!api || oauthBusy) {
      if (!api) setError("Kaggle browser sign-in is available only in the desktop app.");
      return;
    }
    const used = new Set(credentialOptions.map((option) => option.credential_env_ref));
    let index = 1;
    while (used.has(`KCP_KAGGLE_MEMBER_${String(index).padStart(2, "0")}`)) index += 1;
    const ref = `KCP_KAGGLE_MEMBER_${String(index).padStart(2, "0")}`;
    setOauthBusy(true);
    setError("");
    const started = await api.start_kaggle_oauth(ref);
    if (!started.ok) {
      setOauthBusy(false);
      setError(started.error || "Could not start Kaggle sign-in.");
      return;
    }
    for (let attempt = 0; attempt < 300; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      const status = await api.get_kaggle_oauth_status();
      if (status.state === "pending") continue;
      setOauthBusy(false);
      if (status.state !== "succeeded") {
        setError(status.error || "Kaggle sign-in did not complete.");
        return;
      }
      setCredentialOptions((current) => [...current, { credential_env_ref: ref, available: true, registered: false }]);
      setCredentialEnvRef(ref);
      setUsername(status.username || "");
      setOwnerName(status.username || "");
      setConsentBy(status.username || "");
      setCredentialVerified(Boolean(status.username));
      return;
    }
    setOauthBusy(false);
    setError("Kaggle sign-in timed out. You can start it again.");
  };

  const inspectCredential = async () => {
    if (!credentialEnvRef.trim()) {
      setError("Choose a saved credential first.");
      return;
    }
    setInspectingCredential(true);
    setCredentialVerified(false);
    setError("");
    try {
      const value = await apiRequest("/api/credentials/inspect", {
        method: "POST",
        body: JSON.stringify({ credential_env_ref: credentialEnvRef.trim() }),
      }, 30000);
      const record = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
      const detectedUsername = textValue(record, ["kaggle_username"]);
      const recommendedOwner = textValue(record, ["recommended_owner_name"], detectedUsername);
      setUsername(detectedUsername);
      setOwnerName(recommendedOwner);
      setConsentBy(textValue(record, ["recommended_consent_confirmed_by"], recommendedOwner));
      setCredentialVerified(true);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not verify this credential.");
    } finally {
      setInspectingCredential(false);
    }
  };

  const addAccount = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    if (!ownerName.trim() || !username.trim() || !credentialEnvRef.trim() || !consentBy.trim() || !consentChecked || !credentialVerified) {
      setError("Complete the owner, account, credential reference, and consent fields.");
      return;
    }
    if (!/^[A-Z_][A-Z0-9_]*$/.test(credentialEnvRef.trim())) {
      setError("Credential reference must be an environment variable name, for example KAGGLE_TOKEN_MINH. Do not paste a token.");
      return;
    }
    if (isOffline) {
      onFinished(`API offline — @${username.trim()} was not added.`);
      return;
    }
    setSubmitting(true);
    try {
      await apiRequest("/api/accounts", {
        method: "POST",
        body: JSON.stringify({
          owner_name: ownerName.trim(),
          kaggle_username: username.trim().replace(/^@/, ""),
          credential_env_ref: credentialEnvRef.trim(),
          consent_confirmed_by: consentBy.trim(),
          consent_note: consentNote.trim(),
        }),
      });
      onFinished(`Account @${username.trim().replace(/^@/, "")} added to the team registry.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not add this account.");
      setSubmitting(false);
    }
  };

  const changeState = async (nextState: "enabled" | "disabled") => {
    if (!account) return;
    if (isOffline) {
      onFinished(`API offline — @${account.username} was not ${nextState}.`);
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await apiRequest(`/api/accounts/${encodeURIComponent(account.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ state: nextState }),
      });
      onFinished(`@${account.username} is now ${nextState}.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not update this account.");
      setSubmitting(false);
    }
  };

  const revoke = async () => {
    if (!account || revokeText !== account.username) return;
    if (isOffline) {
      onFinished(`API offline — @${account.username} was not revoked.`);
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await apiRequest(`/api/accounts/${encodeURIComponent(account.id)}/revoke`, { method: "POST" });
      onFinished(`Access for @${account.username} was permanently revoked.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not revoke this account.");
      setSubmitting(false);
    }
  };

  const reconcile = async () => {
    if (!account || reconcileText !== account.username) return;
    if (isOffline) {
      onFinished(`API offline — @${account.username} was not reconciled.`);
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const data = await apiRequest(`/api/accounts/${encodeURIComponent(account.id)}/reconcile`, {
        method: "POST",
        body: JSON.stringify({ confirmed: true, note: "Remote Kaggle state manually checked from dashboard" }),
      });
      const record = data && typeof data === "object" ? data as Record<string, unknown> : {};
      const reconciledCount = numberValue(record, ["reconciled_job_count"], 0);
      onFinished(`@${account.username} reconciled. ${reconciledCount} local job${reconciledCount === 1 ? "" : "s"} updated.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not reconcile this account.");
      setSubmitting(false);
    }
  };

  const syncQuota = async () => {
    if (!account || isOffline) return;
    setSubmitting(true);
    setError("");
    try {
      await apiRequest(`/api/accounts/${encodeURIComponent(account.id)}/quota/sync`, {
        method: "POST",
        body: JSON.stringify({}),
      }, 30000);
      onFinished(`Official Kaggle quota synced for @${account.username}.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not sync official Kaggle quota.");
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="account-dialog" role="dialog" aria-modal="true" aria-labelledby="account-dialog-title">
        <header className="composer-header">
          <div>
            <p className="section-label">{account ? "ACCOUNT CONTROL" : "MEMBER ONBOARDING"}</p>
            <h2 id="account-dialog-title">{account ? `Manage @${account.username}` : "Connect a team account."}</h2>
            <p>{account ? "Enable, pause, or revoke this owner’s access." : "Choose a saved token reference; Kaggle identity is detected automatically."}</p>
          </div>
          <button className="close-button" onClick={onClose} aria-label="Close account dialog">×</button>
        </header>

        {account ? (
          <div className="manage-account-body">
            {isOffline && <div className="demo-form-banner"><strong>API offline</strong><span>Account controls are unavailable until the real control plane is connected.</span></div>}
            <div className="managed-account-summary">
              <span className="account-avatar" aria-hidden="true">{account.owner.slice(0, 1)}</span>
              <div><strong>{account.owner}</strong><span>@{account.username}</span></div>
              <span className={`control-state control-${account.state === "blocked" ? "blocked" : controlState}`}>{account.state === "blocked" ? "blocked" : controlState}</span>
            </div>
            <dl className="manage-account-facts">
              <div><dt>Official GPU</dt><dd>{quotaText(account.gpuQuota)}<small>Kaggle API</small></dd></div>
              <div><dt>Official TPU</dt><dd>{quotaText(account.tpuQuota)}<small>Kaggle API</small></dd></div>
              <div><dt>Active jobs</dt><dd>{account.activeRuns} / {account.maxParallel}</dd></div>
            </dl>

            <section className="account-action-section">
              <div><h3>Official Kaggle quota</h3><p>{account.quotaSyncError ? `Last sync failed: ${account.quotaSyncError}` : account.quotaSyncedAt ? `Synced ${account.quotaSyncedAt}. Refreshes at ${account.quotaRefreshAt || "Kaggle schedule"}.` : "Waiting for the first Kaggle API sync."}</p></div>
              <button className="button button-quiet" disabled={submitting || isOffline} onClick={() => void syncQuota()}>Sync now</button>
            </section>

            {account.remoteReconciliationRequired && (
              <section className="safety-action-section safety-reconcile">
                <div><span className="safety-kicker">REMOTE STATE UNKNOWN</span><h3>Reconcile before assigning work</h3><p>A local monitor stopped while Kaggle may still be running. Check the kernel on Kaggle first, then confirm here to unblock this account.</p></div>
                <label><span>After checking Kaggle, type <strong>{account.username}</strong></span><input value={reconcileText} onChange={(event) => setReconcileText(event.target.value)} autoComplete="off" /></label>
                <button className="button button-primary" disabled={submitting || reconcileText !== account.username} onClick={() => void reconcile()}>Confirm reconciliation</button>
              </section>
            )}

            {controlState !== "revoked" && (
              <section className="account-action-section">
                <div><h3>{controlState === "disabled" ? "Enable scheduling" : "Pause new scheduling"}</h3><p>{controlState === "disabled" ? "Allow new experiments to be assigned to this owner." : "Block only new assignments and queued jobs. Work already running on Kaggle is left to complete."}</p></div>
                <button className="button button-quiet" disabled={submitting} onClick={() => void changeState(controlState === "disabled" ? "enabled" : "disabled")}>{controlState === "disabled" ? "Enable account" : "Disable account"}</button>
              </section>
            )}

            <section className="danger-zone">
              <h3>Revoke access</h3>
              <p>This cannot be reversed from the dashboard. The credential reference is detached and the account can no longer receive jobs.</p>
              {controlState === "revoked" ? <strong className="already-revoked">Access already revoked</strong> : <>
                <label><span>Type <strong>{account.username}</strong> to confirm</span><input value={revokeText} onChange={(event) => setRevokeText(event.target.value)} autoComplete="off" /></label>
                <button className="button button-danger" disabled={submitting || revokeText !== account.username} onClick={() => void revoke()}>Permanently revoke</button>
              </>}
            </section>
            {error && <p className="form-error" role="alert">{error}</p>}
            <footer className="composer-footer"><button className="button button-quiet" onClick={onClose}>Close</button></footer>
          </div>
        ) : (
          <form className="account-form" onSubmit={addAccount}>
            {isOffline && <div className="demo-form-banner"><strong>API offline</strong><span>No member can be saved until the real control plane is connected.</span></div>}
            <div className="oauth-member-entry"><div><strong>New member</strong><span>Use Kaggle OAuth to detect the account without copying a password or API token.</span></div><button type="button" className="button button-primary" disabled={oauthBusy || isOffline} onClick={() => void signInWithKaggle()}>{oauthBusy ? "Waiting for browser…" : "Sign in with Kaggle"}</button></div>
            <div className="account-form-grid">
              <label className="wide"><span>Saved Kaggle credential</span><div className="credential-detect-row"><select value={credentialEnvRef} onChange={(event) => { setCredentialEnvRef(event.target.value); setCredentialVerified(false); }}><option value="">Select a credential</option>{credentialOptions.map((option) => <option key={option.credential_env_ref} value={option.credential_env_ref} disabled={!option.available || option.registered}>{option.credential_env_ref}{option.registered ? " (already connected)" : !option.available ? " (unavailable)" : ""}</option>)}</select><button type="button" className="button button-quiet" onClick={() => void inspectCredential()} disabled={!credentialEnvRef || inspectingCredential}>{inspectingCredential ? "Detecting…" : "Detect account"}</button></div><small>Only the saved reference is shown here; the token never enters the browser.</small></label>
              <label><span>Recommended owner name</span><input value={ownerName} onChange={(event) => setOwnerName(event.target.value)} placeholder="Detected from Kaggle" disabled={!credentialVerified} /></label>
              <label><span>Kaggle username</span><input value={username} readOnly placeholder="Detected automatically" autoCapitalize="none" /></label>
              <label><span>Consent confirmed by</span><input value={consentBy} onChange={(event) => setConsentBy(event.target.value)} placeholder="Account owner name" /></label>
              <label><span>Quota source</span><input value="Official Kaggle API (GPU + TPU)" readOnly /></label>
              <label className="wide"><span>Consent note</span><textarea value={consentNote} onChange={(event) => setConsentNote(event.target.value)} rows={3} /></label>
            </div>
            <label className="consent-check"><input type="checkbox" checked={consentChecked} onChange={(event) => setConsentChecked(event.target.checked)} /><span>I confirm this owner explicitly permitted their account to join this team workspace.</span></label>
            {error && <p className="form-error" role="alert">{error}</p>}
            <footer className="composer-footer"><button type="button" className="button button-quiet" onClick={onClose}>Cancel</button><button type="submit" className="button button-primary" disabled={submitting || isOffline || !credentialVerified}>{submitting ? "Connecting…" : isOffline ? "API offline" : !credentialVerified ? "Detect account first" : "Connect account"}</button></footer>
          </form>
        )}
      </section>
    </div>
  );
}

function BatchComposer({ accounts, connection, onClose, onCreated }: {
  accounts: Account[];
  connection: ConnectionState;
  onClose: () => void;
  onCreated: (message: string) => void;
}) {
  const assignable = useMemo(
    () => accounts.filter((item) => item.state !== "offline" && item.state !== "blocked" && item.controlState !== "disabled" && item.controlState !== "revoked"),
    [accounts],
  );
  const newDraft = useCallback((index: number): DraftExperiment => ({
    localId: `${Date.now()}-${index}`,
    name: `Experiment ${index + 1}`,
    accountId: assignable[index % Math.max(assignable.length, 1)]?.id || "",
    sourcePath: "",
    kernelSlug: `team-smoke-${String(index + 1).padStart(2, "0")}`,
    accelerator: "cpu",
    machineShape: "",
  }), [assignable]);
  const [batchName, setBatchName] = useState("experiment-batch");
  const [drafts, setDrafts] = useState<DraftExperiment[]>(() => [newDraft(0)]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [sourceBrowserDraftId, setSourceBrowserDraftId] = useState<string | null>(null);
  const [sourceListing, setSourceListing] = useState<SourceListing | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);

  const browseSource = async (draftId: string, path?: string) => {
    setSourceBrowserDraftId(draftId);
    setSourceLoading(true);
    setError("");
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : "";
      const value = await apiRequest(`/api/sources${query}`);
      setSourceListing(value as SourceListing);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not browse source folders.");
      setSourceBrowserDraftId(null);
    } finally {
      setSourceLoading(false);
    }
  };

  const updateDraft = (id: string, patch: Partial<DraftExperiment>) => {
    setDrafts((current) => current.map((draft) => draft.localId === id ? { ...draft, ...patch } : draft));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    if (!batchName.trim() || drafts.some((item) => !item.name.trim() || !item.accountId || !item.sourcePath.trim() || !item.kernelSlug.trim())) {
      setError("Add a batch name and complete every experiment assignment.");
      return;
    }
    if (connection !== "live") {
      onCreated(`API offline — ${drafts.length} experiments were not submitted.`);
      return;
    }
    setSubmitting(true);
    try {
      await apiRequest("/api/batches", {
        method: "POST",
        body: JSON.stringify({
          name: batchName.trim(),
          jobs: drafts.map(({ name, accountId, sourcePath, kernelSlug, accelerator, machineShape }) => ({
            account_id: accountId,
            experiment_name: name.trim(),
            source_dir: sourcePath.trim(),
            kernel_slug: kernelSlug.trim(),
            metadata: { accelerator, ...(machineShape ? { machine_shape: machineShape } : {}) },
          })),
        }),
      });
      onCreated(`Batch “${batchName.trim()}” submitted with ${drafts.length} experiments.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not submit this batch.");
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="composer" role="dialog" aria-modal="true" aria-labelledby="composer-title">
        <header className="composer-header">
          <div><p className="section-label">BATCH COMPOSER</p><h2 id="composer-title">Dispatch in parallel.</h2><p>Each experiment has an explicit owner and Kaggle account.</p></div>
          <button className="close-button" onClick={onClose} aria-label="Close batch composer">×</button>
        </header>

        <form onSubmit={submit}>
          {connection !== "live" && <div className="demo-form-banner"><strong>API offline</strong><span>Batch submission is disabled until every control-plane endpoint is online.</span></div>}
          <label className="batch-name-field"><span>Batch name</span><input value={batchName} onChange={(event) => setBatchName(event.target.value)} placeholder="family-sprint-08" /></label>

          <div className="draft-list">
            <div className="draft-list-head"><span>{drafts.length} of 10 experiments</span><span>{new Set(drafts.map((item) => item.accountId).filter(Boolean)).size} accounts assigned</span></div>
            {drafts.map((draft, index) => {
              const owner = accounts.find((item) => item.id === draft.accountId);
              return (
                <fieldset className="draft-row" key={draft.localId}>
                  <legend className="sr-only">Experiment {index + 1}</legend>
                  <span className="draft-index">{String(index + 1).padStart(2, "0")}</span>
                  <label className="draft-field draft-title"><span>Experiment name</span><input value={draft.name} onChange={(event) => updateDraft(draft.localId, { name: event.target.value })} /></label>
                  <label className="draft-field draft-source"><span>Source folder</span><div className="source-input-row"><input value={draft.sourcePath} onChange={(event) => updateDraft(draft.localId, { sourcePath: event.target.value })} placeholder="Choose from SourceRoot" /><button type="button" onClick={() => void browseSource(draft.localId, draft.sourcePath || undefined)}>Browse</button></div></label>
                  <label className="draft-field draft-kernel"><span>Kernel slug</span><input value={draft.kernelSlug} onChange={(event) => updateDraft(draft.localId, { kernelSlug: event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") })} /></label>
                  <label className="draft-field draft-account"><span>Owner / Kaggle account</span><select value={draft.accountId} onChange={(event) => updateDraft(draft.localId, { accountId: event.target.value })}>
                    <option value="">Select owner</option>
                    {accounts.map((account) => {
                      const unavailable = account.state === "offline" || account.state === "blocked";
                      const reason = account.remoteReconciliationRequired
                        ? "reconciliation required"
                        : account.state;
                      return <option key={account.id} value={account.id} disabled={unavailable}>{account.owner} · @{account.username}{unavailable ? ` (${reason})` : ""}</option>;
                    })}
                  </select><small>{owner ? `GPU ${quotaText(owner.gpuQuota)} · TPU ${quotaText(owner.tpuQuota)}` : "Assignment required"}</small></label>
                  <label className="draft-field draft-accelerator"><span>Accelerator</span><select value={draft.accelerator} onChange={(event) => { const accelerator = event.target.value; updateDraft(draft.localId, { accelerator, machineShape: accelerator === "gpu" ? "NvidiaTeslaT4" : accelerator === "tpu" ? "TpuV38" : "" }); }}><option value="gpu">GPU · Tesla T4</option><option value="tpu">TPU · V3-8</option><option value="cpu">CPU</option></select></label>
                  <button type="button" className="remove-draft" onClick={() => setDrafts((current) => current.filter((item) => item.localId !== draft.localId))} disabled={drafts.length === 1} aria-label={`Remove ${draft.name}`}>×</button>
                </fieldset>
              );
            })}
          </div>

          <div className="composer-add-row">
            <button type="button" className="add-experiment" onClick={() => setDrafts((current) => current.length < 10 ? [...current, newDraft(current.length)] : current)} disabled={drafts.length >= 10}>＋ Add experiment</button>
            <span>One owner per job · up to 2 concurrent runs per account</span>
          </div>
          {error && <p className="form-error" role="alert">{error}</p>}
          <footer className="composer-footer">
            <button type="button" className="button button-quiet" onClick={onClose}>Cancel</button>
            <button type="submit" className="button button-primary" disabled={submitting || connection !== "live"}>{submitting ? "Submitting…" : connection !== "live" ? "API offline" : `Launch ${drafts.length} experiments`}</button>
          </footer>
        </form>
        {sourceBrowserDraftId && (
          <div className="source-browser-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setSourceBrowserDraftId(null); }}>
            <section className="source-browser" role="dialog" aria-modal="true" aria-labelledby="source-browser-title">
              <header><div><p className="section-label">SOURCE ROOT</p><h3 id="source-browser-title">Choose notebook folder</h3></div><button type="button" className="close-button" onClick={() => setSourceBrowserDraftId(null)} aria-label="Close source browser">×</button></header>
              {sourceLoading || !sourceListing ? <p className="source-loading">Loading folders…</p> : <>
                <code className="source-current">{sourceListing.current}</code>
                <div className="source-browser-actions"><button type="button" className="button button-quiet" disabled={!sourceListing.parent} onClick={() => sourceListing.parent && void browseSource(sourceBrowserDraftId, sourceListing.parent)}>← Parent</button><button type="button" className="button button-primary" disabled={!sourceListing.selectable} onClick={() => { updateDraft(sourceBrowserDraftId, { sourcePath: sourceListing.current }); setSourceBrowserDraftId(null); }}>Use this folder</button></div>
                <div className="source-directory-list">{sourceListing.directories.map((directory) => <button type="button" key={directory.path} onClick={() => void browseSource(sourceBrowserDraftId, directory.path)}><span>▸ {directory.name}</span>{directory.has_kernel_metadata && <small>Notebook ready</small>}</button>)}{!sourceListing.directories.length && <p>No child folders.</p>}</div>
                {!sourceListing.selectable && <p className="source-hint">Open a folder containing <code>kernel-metadata.json</code> to select it.</p>}
              </>}
            </section>
          </div>
        )}
      </section>
    </div>
  );
}

function RunDrawer({ run, connection, onClose, onAction, onRefresh }: {
  run: Run;
  connection: ConnectionState;
  onClose: () => void;
  onAction: (run: Run, action: "cancel" | "retry" | "result") => void;
  onRefresh: () => void;
}) {
  const [olderLogs, setOlderLogs] = useState<string[]>([]);
  const [visibleLogLines, setVisibleLogLines] = useState(100);
  const [beforeId, setBeforeId] = useState(run.logBeforeId);
  const [hasOlderLogs, setHasOlderLogs] = useState(run.hasOlderLogs === true);
  const [loadingOlderLogs, setLoadingOlderLogs] = useState(false);
  const [downloading, setDownloading] = useState<"logs" | "result" | null>(null);
  const [downloadError, setDownloadError] = useState("");
  const logs = [...olderLogs, ...run.logs];
  const shownLogs = logs.slice(-visibleLogLines);

  const loadOlderLogs = async () => {
    if (!beforeId || loadingOlderLogs) return;
    setLoadingOlderLogs(true);
    setDownloadError("");
    try {
      const value = await apiRequest(`/api/jobs/${encodeURIComponent(run.id)}/events?before_id=${beforeId}&limit=200`);
      const record = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
      const events = asList(record, ["events"]);
      const older = events.flatMap(eventLogLines);
      setOlderLogs((current) => [...older, ...current]);
      setVisibleLogLines((current) => current + older.length);
      setBeforeId(numberValue(record, ["before_id"], 0) || undefined);
      setHasOlderLogs(record.has_more === true);
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : "Could not load older logs.");
    } finally {
      setLoadingOlderLogs(false);
    }
  };

  const download = async (kind: "logs" | "result") => {
    setDownloading(kind);
    setDownloadError("");
    try {
      const suffix = kind === "logs" ? "logs/download" : "result/download";
      await downloadApiFile(`/api/jobs/${encodeURIComponent(run.id)}/${suffix}`);
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : "Download failed.");
    } finally {
      setDownloading(null);
    }
  };

  return (
    <div className="drawer-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <aside className="run-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
        <header className="drawer-header">
          <div><p className="section-label">RUN DETAIL · {run.id}</p><h2 id="drawer-title">{run.name}</h2><StatusBadge status={run.status} /></div>
          <button className="close-button" onClick={onClose} aria-label="Close run detail">×</button>
        </header>
        {connection !== "live" && <p className="drawer-demo">API offline — actions below cannot reach Kaggle.</p>}
        {run.remoteMayBeRunning && (
          <div className="remote-warning" role="alert">
            <strong>Kaggle may still be running this kernel</strong>
            <span>{run.cancelSemantics === "local_monitor_stop_requested" ? "Cancel stopped the local monitor only." : "The remote state could not be confirmed."} Check Kaggle, then reconcile the assigned account before sending it more work.</span>
          </div>
        )}
        <dl className="run-facts">
          <div><dt>Owner</dt><dd>{run.owner} <small>@{run.username}</small></dd></div>
          <div><dt>Accelerator</dt><dd>{run.accelerator}</dd></div>
          <div><dt>Runtime</dt><dd>{run.duration}</dd></div>
          <div><dt>Machine shape</dt><dd>{run.machineShape || <EmptyMark />}</dd></div>
          <div><dt>Metric</dt><dd>{run.metric || <EmptyMark />}</dd></div>
          {run.runtimeInfo && (
            <div className="wide runtime-manifest">
              <dt>Resolved runtime</dt>
              <dd>
                {Object.entries(run.runtimeInfo).map(([key, value]) => (
                  <span key={key}><small>{key.replaceAll("_", " ")}</small><code>{value}</code></span>
                ))}
              </dd>
            </div>
          )}
          <div className="wide"><dt>Source</dt><dd><code>{run.sourcePath}</code></dd></div>
          {run.outputDir && <div className="wide"><dt>Output directory</dt><dd><code>{run.outputDir}</code></dd></div>}
          {run.resultSummary && <div className="wide"><dt>Result preview</dt><dd><ResultPreview value={run.resultSummary} /></dd></div>}
        </dl>
        <div className="drawer-progress"><div><span>Run progress</span><strong>{run.progress}%</strong></div><div role="progressbar" aria-label="Run progress" aria-valuenow={run.progress} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${run.progress}%` }} /></div></div>
        <section className="log-panel" aria-labelledby="log-title"><header><h3 id="log-title">Live output</h3><div><span>Kaggle sync ≤30s · {logs.length} loaded</span><button type="button" onClick={onRefresh} disabled={connection !== "live"}>Refresh</button></div></header>{hasOlderLogs && <button type="button" className="load-more-logs load-older-logs" disabled={loadingOlderLogs} onClick={() => void loadOlderLogs()}>{loadingOlderLogs ? "Loading…" : "Load 200 older lines"}</button>}<pre>{shownLogs.map((line, index) => <code key={`${line}-${index}`}><span>{String(Math.max(1, logs.length - shownLogs.length + index + 1)).padStart(2, "0")}</span>{line}</code>)}</pre>{visibleLogLines < logs.length && <button type="button" className="load-more-logs" onClick={() => setVisibleLogLines((current) => current + 200)}>Render 200 more loaded lines</button>}</section>
        {downloadError && <p className="drawer-download-error" role="alert">{downloadError}</p>}
        <footer className="drawer-actions">
          {(run.status === "running" || run.status === "queued") && <button className="button button-danger" onClick={() => onAction(run, "cancel")}>Cancel run</button>}
          {(run.status === "failed" || run.status === "cancelled") && <button className="button button-primary" onClick={() => onAction(run, "retry")}>Retry run</button>}
          {run.status === "succeeded" && <button className="button button-primary" onClick={() => onAction(run, "result")}>Get results</button>}
          <button className="button button-quiet" disabled={downloading !== null || connection !== "live"} onClick={() => void download("logs")}>{downloading === "logs" ? "Preparing logs…" : "Download logs"}</button>
          <button className="button button-quiet" disabled={downloading !== null || connection !== "live" || run.status !== "succeeded"} onClick={() => void download("result")}>{downloading === "result" ? "Preparing ZIP…" : "Download results (.zip)"}</button>
          <button className="button button-quiet" onClick={onClose}>Close</button>
        </footer>
      </aside>
    </div>
  );
}
