"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import DatasetLibrary from "./dataset-library";
import ArtifactArchivePanel from "./artifact-archive-panel";

type Account = { id: string; username: string; controlState?: string; credentialAvailable?: boolean };
type Selection = { selection_id: string; file_count: number; bytes: number; files: { name: string; bytes: number }[]; nested_folders_zipped?: boolean };
type Operation = { id: string; kind: string; account_id: string; dataset_ref: string; title: string; is_private: boolean; status: string; phase: string; file_count: number; bytes: number; error?: string; visibility_verified?: boolean; readers_verified?: boolean; cli_status?: string; created_at: string };
type Remote = { dataset_ref: string; title: string; description: string; is_private: boolean | null; license: string; readers: string[]; readers_available: boolean; files: string; cli_status: string };
type Props = { accounts: Account[]; desktopAvailable: boolean; request: (path: string, init?: RequestInit, timeoutMs?: number) => Promise<unknown> };

function size(bytes: number) {
  return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MiB` : `${bytes.toLocaleString()} bytes`;
}
function message(error: unknown) { return error instanceof Error ? error.message : "Dataset request failed"; }

export default function DatasetsPanel({ accounts, desktopAvailable, request }: Props) {
  const [operations, setOperations] = useState<Operation[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [mode, setMode] = useState<"upload" | "settings" | null>(null);
  const [accountId, setAccountId] = useState("");
  const [title, setTitle] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [license, setLicense] = useState("other");
  const [isPrivate, setIsPrivate] = useState(true);
  const [confirmPublic, setConfirmPublic] = useState(false);
  const [readers, setReaders] = useState("");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [source, setSource] = useState("");
  const [ref, setRef] = useState("");
  const [remote, setRemote] = useState<Remote | null>(null);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const dialog = useRef<HTMLDivElement>(null);
  const eligible = accounts.filter(a => a.controlState === "enabled" && a.credentialAvailable !== false);
  const selectedAccount = accounts.find(a => a.id === accountId);
  const effectiveAccount = accountId || eligible[0]?.id || "";
  const ownerAccount = remote && eligible.find(a => a.username.toLowerCase() === remote.dataset_ref.split("/")[0].toLowerCase());
  const canEdit = mode !== "settings" || selectedAccount?.username.toLowerCase() === remote?.dataset_ref.split("/")[0].toLowerCase();

  const refresh = useCallback(async () => {
    try {
      const result = await request("/api/datasets/operations") as { operations: Operation[] };
      setOperations(result.operations);
      setError("");
    } catch (error) { setError(message(error)); }
  }, [request]);
  useEffect(() => {
    const start = window.setTimeout(() => { void refresh(); }, 0);
    const timer = window.setInterval(() => { void refresh(); }, 5000);
    return () => { window.clearTimeout(start); window.clearInterval(timer); };
  }, [refresh]);
  useEffect(() => {
    if (!mode) return;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLElement>("button, input, select, textarea")?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) setMode(null);
      if (event.key !== "Tab") return;
      const targets = Array.from(dialog.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]") || []);
      const first = targets[0], last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => { document.removeEventListener("keydown", keydown); previous?.focus(); };
  }, [mode, busy]);

  function startUpload() {
    setAccountId(effectiveAccount); setTitle(""); setSlug(""); setDescription(""); setLicense("other");
    setIsPrivate(true); setConfirmPublic(false); setReaders(""); setSelection(null); setSource("");
    setRemote(null); setDialogError(""); setIdempotencyKey(crypto.randomUUID()); setMode("upload");
  }
  async function choose(folder: boolean) {
    setBusy(true); setDialogError("");
    try {
      const result = await window.pywebview?.api.choose_dataset_files(folder);
      if (result?.cancelled) return;
      if (!result?.ok) throw new Error(result?.error || "Open the desktop app to select files");
      setSelection(result as Selection & { ok: boolean });
    } catch (error) { setDialogError(message(error)); }
    finally { setBusy(false); }
  }
  async function prepare() {
    setBusy(true); setDialogError("");
    try { setSelection(await request("/api/datasets/prepare", { method: "POST", body: JSON.stringify({ source_dir: source }) }, 60000) as Selection); }
    catch (error) { setDialogError(message(error)); }
    finally { setBusy(false); }
  }
  async function inspect(event?: FormEvent, operation?: Pick<Operation, "account_id" | "dataset_ref">) {
    event?.preventDefault(); setBusy(true); setError(""); setNotice("");
    const chosenAccount = operation?.account_id || effectiveAccount;
    const chosenRef = operation?.dataset_ref || ref.trim();
    try {
      const result = await request("/api/datasets/inspect", { method: "POST", body: JSON.stringify({ account_id: chosenAccount, dataset_ref: chosenRef }) }, 390000) as Remote;
      if (typeof result.is_private !== "boolean") throw new Error("Kaggle did not return dataset visibility");
      setRemote(result); setAccountId(chosenAccount); setTitle(result.title); setDescription(result.description);
      setLicense(result.license); setIsPrivate(result.is_private); setConfirmPublic(false); setReaders(result.readers.join(", "));
      setDialogError(""); setIdempotencyKey(crypto.randomUUID()); setMode("settings");
    } catch (error) { setError(message(error)); }
    finally { setBusy(false); }
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setDialogError("");
    const datasetRef = mode === "settings" ? remote?.dataset_ref : `${selectedAccount?.username}/${slug}`;
    try {
      const result = await request("/api/datasets/operations", { method: "POST", body: JSON.stringify({
        kind: mode, account_id: accountId, dataset_ref: datasetRef, title, description, license,
        is_private: isPrivate, confirm_public: confirmPublic, readers: readers.split(/[\s,]+/).filter(Boolean),
        selection_id: selection?.selection_id, idempotency_key: idempotencyKey,
      }) }) as { operation: Operation };
      setOperations(old => [result.operation, ...old.filter(o => o.id !== result.operation.id)]);
      setNotice(`${datasetRef}: ${result.operation.phase}. You can follow progress below.`); setMode(null);
    } catch (error) { setDialogError(message(error)); }
    finally { setBusy(false); }
  }

  return <section id="datasets" className="section-block section-anchor" aria-labelledby="datasets-title">
    <div className="section-heading">
      <div><p className="section-label">DATASET LIBRARY</p><h2 id="datasets-title">Upload. Share. Control access.</h2></div>
      <div className="account-heading-actions"><button className="button button-quiet" onClick={() => void refresh()}>Refresh</button><button className="button button-primary" disabled={!eligible.length || busy} onClick={startUpload}>＋ Upload dataset</button></div>
    </div>
    <form className="dataset-lookup" onSubmit={event => void inspect(event)}>
      <label><span>Access account</span><select aria-label="Dataset access account" value={effectiveAccount} onChange={e => setAccountId(e.target.value)} required>{!eligible.length && <option value="">Connect an account first</option>}{eligible.map(a => <option key={a.id} value={a.id}>{a.username}</option>)}</select></label>
      <label className="dataset-ref-field"><span>Existing dataset</span><input placeholder="owner/dataset-slug" aria-label="Existing dataset reference" value={ref} onChange={e => setRef(e.target.value)} required /></label>
      <button className="button button-quiet" disabled={busy || !effectiveAccount}>{busy && !mode ? "Reading Kaggle…" : "Inspect & manage"}</button>
    </form>
    {error && <p className="dataset-error" role="alert">{error}</p>}
    {notice && <p className="dataset-notice" role="status">{notice}</p>}
    <ArtifactArchivePanel request={request} />
    <DatasetLibrary accounts={accounts} request={request} inspect={operation => { void inspect(undefined, operation); }} />
    {!operations.length ? <div className="empty-state"><strong>No dataset operations yet.</strong><span>Upload local files or inspect an existing dataset to manage visibility and readers.</span></div> : <div className="dataset-operations">
      {operations.map(o => <article className="dataset-operation" key={o.id}>
        <div><a href={`https://www.kaggle.com/datasets/${o.dataset_ref}`} target="_blank" rel="noreferrer">{o.dataset_ref} ↗</a><span>{o.kind === "upload" ? `${o.file_count} files · ${size(o.bytes)}` : "Settings update"} · {accounts.find(a => a.id === o.account_id)?.username || "Account unavailable"}</span><small>{new Date(o.created_at).toLocaleString()}</small></div>
        <div><strong className={`dataset-state dataset-${o.status}`}>{o.status}</strong><span>{o.phase}</span><small>{o.is_private ? "Private" : "Public"}{o.visibility_verified === false ? " · unverified" : ""}</small></div>
        <button className="button button-quiet" disabled={busy || !["succeeded", "failed", "interrupted"].includes(o.status)} onClick={() => void inspect(undefined, o)}>Inspect</button>
        {o.error && <p className="dataset-error" role="alert">{o.error}</p>}
        {o.status === "succeeded" && o.readers_verified === false && <p className="dataset-notice">Reader update sent. Kaggle did not return permissions for verification; check them on Kaggle.</p>}
        {o.cli_status && <pre className="dataset-cli-status">{o.cli_status}</pre>}
      </article>)}
    </div>}
    {mode && <div className="modal-layer">
      <div ref={dialog} className="account-dialog dataset-dialog" role="dialog" aria-modal="true" aria-labelledby="dataset-dialog-title">
        <header className="composer-header"><div><p className="section-label">DATASETS</p><h2 id="dataset-dialog-title">{mode === "upload" ? "Upload a dataset" : "Dataset settings"}</h2>{remote && <p>{remote.dataset_ref}</p>}</div><button type="button" className="icon-button" aria-label="Close dataset dialog" disabled={busy} onClick={() => setMode(null)}>×</button></header>
        <form className="dataset-form" onSubmit={event => void submit(event)}>
          <div className="account-form-grid">
            <label><span>{mode === "settings" ? "Access account" : "Owner account"}</span><select value={accountId} onChange={e => setAccountId(e.target.value)} required disabled={mode === "settings" || busy}>{eligible.map(a => <option key={a.id} value={a.id}>{a.username}</option>)}</select></label>
            {mode === "upload" && <label><span>Dataset slug</span><input value={slug} minLength={6} maxLength={50} pattern="[A-Za-z0-9_-]{6,50}" placeholder="my-research-data" required onChange={e => setSlug(e.target.value)} /></label>}
            <label className="wide"><span>Title</span><input value={title} minLength={6} maxLength={50} required onChange={e => setTitle(e.target.value)} /></label>
            <label className="wide"><span>Description</span><textarea rows={3} value={description} maxLength={50000} onChange={e => setDescription(e.target.value)} /></label>
            <label><span>Visibility</span><select value={isPrivate ? "private" : "public"} onChange={e => { setIsPrivate(e.target.value === "private"); setConfirmPublic(false); }}><option value="private">Private — invited accounts only</option><option value="public">Public — anyone can access</option></select></label>
            <label><span>License{mode === "settings" ? " (preserved)" : ""}</span>{mode === "upload" ? <select value={license} onChange={e => setLicense(e.target.value)}><option value="other">Other / custom terms</option>{["CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "ODC-BY-1.0", "ODbL-1.0"].map(l => <option key={l}>{l}</option>)}</select> : <input readOnly value={license} />}</label>
            <label className="wide"><span>Reader usernames (comma separated)</span><input value={readers} onChange={e => setReaders(e.target.value)} placeholder="teammate01, teammate02" /><small>Readers can use the dataset; they cannot edit it. Existing writer/admin roles are preserved.</small></label>
          </div>
          <div className="dataset-reader-shortcuts">{eligible.filter(a => a.id !== accountId).map(a => <button type="button" className="button button-quiet" key={a.id} onClick={() => setReaders(old => Array.from(new Set([...old.split(/[\s,]+/).filter(Boolean), a.username])).join(", "))}>＋ {a.username}</button>)}</div>
          {mode === "upload" && <div className="dataset-selection"><strong>Files to upload</strong>{desktopAvailable ? <div className="settings-actions"><button type="button" className="button button-quiet" disabled={busy} onClick={() => void choose(false)}>Choose files…</button><button type="button" className="button button-quiet" disabled={busy} onClick={() => void choose(true)}>Choose folder…</button></div> : <><label className="dataset-path-label">Dedicated folder inside the configured source root<input value={source} onChange={e => setSource(e.target.value)} placeholder="Full folder path" /></label><button type="button" className="button button-quiet" disabled={busy || !source} onClick={() => void prepare()}>Preview folder</button></>}
            {selection && <><p>{selection.file_count} files · {size(selection.bytes)}</p><ul>{selection.files.slice(0, 12).map(f => <li key={f.name}>{f.name} <span>{size(f.bytes)}</span></li>)}</ul>{selection.file_count > 12 && <p>…and {selection.file_count - 12} more files</p>}{selection.nested_folders_zipped && <p>Nested folders will be uploaded as ZIP archives, preserving their structure.</p>}</>}
          </div>}
          {mode === "settings" && remote && <details className="dataset-remote-details"><summary>Files & Kaggle status</summary><pre>{remote.cli_status}{"\n"}{remote.files}</pre></details>}
          {!canEdit && remote && <div className="dataset-notice">This account can inspect the dataset. Select its owner to edit settings.{ownerAccount && <button type="button" className="button button-quiet" disabled={busy} onClick={() => void inspect(undefined, { account_id: ownerAccount.id, dataset_ref: remote.dataset_ref })}>Inspect as {ownerAccount.username}</button>}</div>}
          {!isPrivate && <label className="consent-check"><input type="checkbox" required checked={confirmPublic} onChange={e => setConfirmPublic(e.target.checked)} /><span>I want this dataset and all of its files to be publicly accessible.</span></label>}
          {dialogError && <p className="dataset-error" role="alert">{dialogError}</p>}
          <div className="dataset-form-footer"><button type="button" className="button button-quiet" disabled={busy} onClick={() => setMode(null)}>Cancel</button><button className="button button-primary" disabled={busy || !canEdit || (mode === "upload" && !selection) || (!isPrivate && !confirmPublic)}>{busy ? "Working…" : mode === "upload" ? "Upload dataset" : "Save settings"}</button></div>
        </form>
      </div>
    </div>}
  </section>;
}
