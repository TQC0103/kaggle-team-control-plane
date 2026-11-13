"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

type Account = { id: string; username: string; controlState?: string; credentialAvailable?: boolean };
type Deletion = { id: string; dataset_ref: string; status: string; phase: string; error?: string };
type Dataset = {
  dataset_ref: string; account_id: string; owner: string; title: string; bytes: number | null;
  is_private: boolean | null; last_updated: string | null; synced_at: string | null; remote_state: string;
  url: string; file_count?: number; upload_status?: string; active_dependencies: { job_id: string; reason: string }[];
  deletion?: Deletion; verification?: { verified_at: string; file_count: number; local_evicted: boolean; local_eviction_pending?: boolean; stale: boolean; source: string };
};
type Library = { datasets: Dataset[]; accounts: Record<string, { status: string; count?: number; checked_at: string; error?: string }>; refreshing: boolean; total_bytes: number; deletions: Deletion[] };
type Props = { accounts: Account[]; request: (path: string, init?: RequestInit, timeoutMs?: number) => Promise<unknown>; inspect: (dataset: { account_id: string; dataset_ref: string }) => void };
const active = (state?: string) => state === "queued" || state === "running";
const size = (bytes: number | null) => bytes === null ? "Unknown" : bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
const errorText = (e: unknown) => e instanceof Error ? e.message : "Dataset library request failed";

export default function DatasetLibrary({ accounts, request, inspect }: Props) {
  const [library, setLibrary] = useState<Library>({ datasets: [], accounts: {}, refreshing: false, total_bytes: 0, deletions: [] });
  const [account, setAccount] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState("size");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<Dataset | null>(null);
  const [confirmRef, setConfirmRef] = useState("");
  const [acknowledge, setAcknowledge] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [deleteKey, setDeleteKey] = useState("");
  const dialog = useRef<HTMLDivElement>(null);
  const refresh = useCallback(async () => {
    try { setLibrary(await request("/api/datasets/library") as Library); setError(""); }
    catch (e) { setError(errorText(e)); }
  }, [request]);
  useEffect(() => {
    const initial = window.setTimeout(() => { void refresh(); }, 0);
    const timer = window.setInterval(() => { void refresh(); }, 5000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [refresh]);
  useEffect(() => {
    if (!selected) return;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLElement>("button, input")?.focus();
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) setSelected(null);
      if (event.key !== "Tab") return;
      const targets = Array.from(dialog.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)") || []);
      if (event.shiftKey && document.activeElement === targets[0]) { event.preventDefault(); targets.at(-1)?.focus(); }
      else if (!event.shiftKey && document.activeElement === targets.at(-1)) { event.preventDefault(); targets[0]?.focus(); }
    };
    document.addEventListener("keydown", listener);
    return () => { document.removeEventListener("keydown", listener); previous?.focus(); };
  }, [selected, busy]);
  async function sync() {
    setBusy(true); setError("");
    try {
      await request("/api/datasets/library/refresh", { method: "POST", body: JSON.stringify(account ? { account_ids: [account] } : {}) });
      setNotice("Reading owned datasets from Kaggle. Existing inventory stays visible while accounts sync.");
      await refresh();
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }
  function openDelete(row: Dataset) {
    setSelected(row); setConfirmRef(""); setAcknowledge(false); setDialogError(""); setDeleteKey(crypto.randomUUID());
  }
  async function remove(event: FormEvent) {
    event.preventDefault(); if (!selected) return;
    setBusy(true); setDialogError("");
    try {
      await request("/api/datasets/library/delete", { method: "POST", body: JSON.stringify({
        account_id: selected.account_id, dataset_ref: selected.dataset_ref, confirm_dataset_ref: confirmRef,
        confirm_delete: true, acknowledge_archive_loss: acknowledge, idempotency_key: deleteKey,
      }) });
      setNotice(`Deletion requested for ${selected.dataset_ref}. It remains pending until Kaggle confirms it is absent.`);
      setSelected(null); await refresh();
    } catch (e) { setDialogError(errorText(e)); }
    finally { setBusy(false); }
  }
  async function checkDeletion(deletion: Deletion) {
    setBusy(true); setError("");
    try {
      await request("/api/datasets/library/deletion-status", { method: "POST", body: JSON.stringify({ deletion_id: deletion.id }) }, 150000);
      await refresh();
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }
  const rows = library.datasets.filter(row => (!account || row.account_id === account) && `${row.title} ${row.dataset_ref} ${row.owner}`.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => sort === "size" ? (b.bytes ?? -1) - (a.bytes ?? -1) : sort === "updated" ? (b.last_updated || "").localeCompare(a.last_updated || "") : a.dataset_ref.localeCompare(b.dataset_ref));
  const eligible = accounts.filter(a => a.controlState === "enabled" && a.credentialAvailable !== false);
  const pending = library.deletions.filter(d => d.status !== "deleted");
  const isAccountUploading = (id: string) => library.datasets.some(d => d.account_id === id && d.upload_status && !["succeeded", "failed", "interrupted"].includes(d.upload_status));

  return <div className="dataset-library" aria-labelledby="dataset-library-title">
    <div className="dataset-library-heading"><div><h3 id="dataset-library-title">Datasets & archived results</h3><p>{library.datasets.length} datasets · {size(library.total_bytes)} reported on Kaggle</p></div>
      <button className="button button-quiet" disabled={busy || library.refreshing || !eligible.length} onClick={() => void sync()}>{library.refreshing ? "Syncing accounts…" : "Sync Kaggle inventory"}</button></div>
    <div className="dataset-library-filters">
      <label><span>Owner account</span><select value={account} onChange={e => setAccount(e.target.value)}><option value="">All accounts</option>{eligible.map(a => <option key={a.id} value={a.id}>{a.username}</option>)}</select></label>
      <label className="dataset-library-search"><span>Search datasets</span><input type="search" value={search} placeholder="Dataset, result or owner…" onChange={e => setSearch(e.target.value)} /></label>
      <label><span>Sort</span><select value={sort} onChange={e => setSort(e.target.value)}><option value="size">Largest first</option><option value="updated">Recently updated</option><option value="name">Dataset name</option></select></label>
    </div>
    <div className="dataset-account-summary">{eligible.map(a => {
      const state = library.accounts[a.id]; const own = library.datasets.filter(d => d.account_id === a.id);
      return <div key={a.id}><strong>{a.username}</strong><span>{own.length} datasets · {size(own.reduce((n, d) => n + (d.bytes || 0), 0))}</span><small>{state?.status || "Not synced"}{state?.checked_at ? ` · ${new Date(state.checked_at).toLocaleString()}` : ""}</small>{state?.error && <span className="dataset-error">{state.error}</span>}</div>;
    })}</div>
    {error && <p className="dataset-error" role="alert">{error}</p>}{notice && <p className="dataset-notice" role="status">{notice}</p>}
    <p className="dataset-library-note">Inventory size is remote storage. Upload completion and a SHA-256 readback report are shown separately. Inspect a dataset to refresh its visibility and files.</p>
    {rows.length ? <div className="dataset-library-scroll"><table className="dataset-library-table"><thead><tr><th scope="col">Dataset / result</th><th scope="col">Account</th><th scope="col">Size</th><th scope="col">Visibility</th><th scope="col">Backup verification</th><th scope="col">Remote state</th><th scope="col">Actions</th></tr></thead><tbody>
      {rows.map(row => {
        const blocked = row.active_dependencies.length > 0 || isAccountUploading(row.account_id) || row.verification?.local_eviction_pending || active(row.deletion?.status) || row.deletion?.status === "uncertain";
        return <tr key={row.dataset_ref}><td><a href={row.url} target="_blank" rel="noreferrer">{row.title} ↗</a><small>{row.dataset_ref}</small>{row.last_updated && <small>{new Date(row.last_updated).toLocaleString()}</small>}</td><td>{row.owner}</td><td className="dataset-size">{size(row.bytes)}{row.file_count !== undefined && <small>{row.file_count} uploaded files</small>}</td><td>{row.is_private === null ? "Not checked" : row.is_private ? "Private" : "Public"}</td>
          <td>{row.verification ? <><strong className={row.verification.stale ? "dataset-error" : "dataset-verified"}>{row.verification.stale ? "Changed since verification" : "SHA-256 readback report"}</strong><small>{row.verification.file_count} files · {new Date(row.verification.verified_at).toLocaleString()}</small><small>{row.verification.local_eviction_pending ? "Local cleanup in progress" : row.verification.local_evicted ? "Local copy removed" : "Local removal not recorded"}</small></> : <span className="dataset-unverified">No readback report</span>}</td>
          <td>{row.deletion?.status === "uncertain" ? "Deletion unconfirmed" : row.remote_state.replaceAll("_", " ")}{row.upload_status && <small>Upload operation: {row.upload_status}</small>}{row.active_dependencies.length > 0 && <small>{row.active_dependencies.length} active job dependencies</small>}</td>
          <td><div className="dataset-row-actions"><button className="button button-quiet" disabled={busy} onClick={() => inspect(row)}>Inspect</button><button className="button dataset-delete-button" disabled={busy || blocked} title={blocked ? "Wait for upload, active jobs or unresolved deletion" : `Delete ${row.dataset_ref} from Kaggle`} onClick={() => openDelete(row)}>Delete…</button></div></td></tr>;
      })}
    </tbody></table></div> : <div className="empty-state"><strong>{search || account ? "No matching datasets." : "No inventory synced yet."}</strong><span>Sync Kaggle inventory to list owned datasets across connected accounts.</span></div>}
    {pending.length > 0 && <div className="dataset-deletion-history"><h4>Deletion status</h4>{pending.slice(-8).reverse().map(d => <div key={d.id}><strong>{d.dataset_ref}</strong><span>{d.status} · {d.phase}</span>{d.error && <small className="dataset-error">{d.error}</small>}{d.status === "uncertain" && <button className="button button-quiet" disabled={busy} onClick={() => void checkDeletion(d)}>Check remote state</button>}</div>)}</div>}
    {selected && <div className="modal-layer"><div ref={dialog} className="account-dialog dataset-delete-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-dataset-title"><header className="composer-header"><div><p className="section-label">KAGGLE DATASET</p><h2 id="delete-dataset-title">Delete this dataset?</h2></div><button type="button" className="icon-button" aria-label="Close delete dataset dialog" disabled={busy} onClick={() => setSelected(null)}>×</button></header>
      <form className="dataset-form" onSubmit={event => void remove(event)}><p>This permanently deletes <strong>{selected.dataset_ref}</strong> from Kaggle using account <strong>{selected.owner}</strong>.</p>
        <p>{size(selected.bytes)} · {selected.verification?.local_evicted ? "The local copy has been removed. This may be the remaining archived copy." : "Existing local files are kept."}</p>
        <label className="dataset-delete-confirm"><span>Type the exact dataset reference to confirm</span><input autoComplete="off" value={confirmRef} onChange={e => setConfirmRef(e.target.value)} placeholder={selected.dataset_ref} required /></label>
        {selected.verification?.local_evicted && <label className="confirmation-check"><input type="checkbox" checked={acknowledge} onChange={e => setAcknowledge(e.target.checked)} required /><span>I understand the local copy was removed and I am deleting the archived copy.</span></label>}
        {dialogError && <p className="dataset-error" role="alert">{dialogError}</p>}<div className="dataset-form-footer"><button type="button" className="button button-quiet" disabled={busy} onClick={() => setSelected(null)}>Keep dataset</button><button className="button dataset-delete-button" disabled={busy || confirmRef !== selected.dataset_ref || (!!selected.verification?.local_evicted && !acknowledge)}>{busy ? "Requesting deletion…" : "Delete from Kaggle"}</button></div></form>
    </div></div>}
  </div>;
}
