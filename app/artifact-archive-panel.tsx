"use client";
import { useCallback, useEffect, useState } from "react";
type Archive = { config: { enabled: boolean }; jobs: { job_id: string; kernel_slug: string; status: string; error?: string; parts: { status: string; dataset_ref: string }[] }[]; active_workers: number };
type Props = { request: (path: string, init?: RequestInit, timeoutMs?: number) => Promise<unknown> };
export default function ArtifactArchivePanel({ request }: Props) {
  const [data, setData] = useState<Archive>({ config: { enabled: false }, jobs: [], active_workers: 0 });
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    try { setData(await request("/api/artifacts/archive") as Archive); setError(""); }
    catch (e) { setError(e instanceof Error ? e.message : "Archive status unavailable"); }
  }, [request]);
  useEffect(() => { const first = window.setTimeout(() => void refresh(), 0); const timer = window.setInterval(() => void refresh(), 10000); return () => { window.clearTimeout(first); window.clearInterval(timer); }; }, [refresh]);
  async function toggle() {
    try { setData(await request("/api/artifacts/archive/config", { method: "POST", body: JSON.stringify({ enabled: !data.config.enabled }) }) as Archive); setError(""); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not update archive setting"); }
  }
  return <section className="dataset-library" aria-labelledby="auto-archive-title">
    <div className="dataset-library-heading"><div><h3 id="auto-archive-title">Automatic private artifact backup</h3><p>Every future completed run · resumes when this app reopens · local originals stay in place</p></div><button className="button button-quiet" aria-pressed={data.config.enabled} onClick={() => void toggle()}>{data.config.enabled ? "Enabled — pause" : "Enable for future runs"}</button></div>
    {error && <p role="alert" className="dataset-error">{error}</p>}
    <p>{data.active_workers} active transfers. Verified means full private remote SHA-256 readback; it does not certify research metrics.</p>
    {data.jobs.length > 0 && <div className="table-scroll"><table><thead><tr><th>Run</th><th>Backup</th><th>Verified parts</th><th>Details</th></tr></thead><tbody>{data.jobs.map(job => <tr key={job.job_id}><td>{job.kernel_slug}</td><td>{job.status.replaceAll("_", " ")}</td><td>{job.parts.filter(p => p.status === "verified").length}/{job.parts.length}</td><td>{job.error || job.parts.map(part => <a key={part.dataset_ref} href={`https://www.kaggle.com/datasets/${part.dataset_ref}`} target="_blank" rel="noreferrer">{part.dataset_ref} </a>)}</td></tr>)}</tbody></table></div>}
  </section>;
}
