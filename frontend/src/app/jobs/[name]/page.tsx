"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import StatusBadge from "@/components/StatusBadge";
import type { JobDetail } from "@/lib/types";

const POLL_INTERVAL_RUNNING_MS = 2000;
const POLL_INTERVAL_IDLE_MS = 8000;

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

export default function JobDetailPage({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = use(params);
  const [job, setJob] = useState<JobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [triggering, setTriggering] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout>;

    async function poll() {
      try {
        const res = await fetch(`/api/jobs/${encodeURIComponent(name)}`);
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.error || body.detail || `Request failed (${res.status})`);
        }
        const data: JobDetail = await res.json();
        if (!cancelled) {
          setJob(data);
          setError(null);
          const nextInterval =
            data.status === "running" ? POLL_INTERVAL_RUNNING_MS : POLL_INTERVAL_IDLE_MS;
          timeoutId = setTimeout(poll, nextInterval);
        }
      } catch (reason) {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "Unexpected error");
          timeoutId = setTimeout(poll, POLL_INTERVAL_IDLE_MS);
        }
      }
    }

    poll();
    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  }, [name]);

  async function handleTrigger() {
    setTriggering(true);
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(name)}/run`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || body.detail || `Trigger failed (${res.status})`);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Trigger failed");
    } finally {
      setTriggering(false);
    }
  }

  return (
    <>
      <header className="border-b border-hairline bg-panel">
        <div className="mx-auto max-w-4xl px-6 py-4">
          <Link
            href="/"
            className="inline-flex items-center gap-2 rounded-lg border border-hairline bg-panel-raised px-3 py-1.5 font-mono text-xs text-ink hover:border-brand-blue hover:text-brand-blue transition-colors"
          >
            <ArrowLeft size={14} />
            Back to all jobs
          </Link>
        </div>
      </header>

      <main className="flex-1 mx-auto w-full max-w-4xl px-6 py-8">
        <h1 className="font-mono text-lg font-semibold break-all text-ink">{name}</h1>

        {error && (
          <div className="mt-4 rounded border border-status-failed/40 bg-status-failed-bg px-4 py-3 text-sm text-status-failed">
            {error}
          </div>
        )}

        {job && (
          <div className="fade-up">
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <StatusBadge status={job.status} />
              <span className="text-sm text-ink-muted">{job.message}</span>
            </div>

            <div className="mt-6 grid grid-cols-2 gap-4 rounded-xl border border-hairline bg-panel p-4 text-sm sm:grid-cols-4">
              <div>
                <div className="text-xs text-ink-dim">Schedule</div>
                <div className="font-mono text-xs mt-1 text-ink">{job.cron_schedule || "manual"}</div>
              </div>
              <div>
                <div className="text-xs text-ink-dim">Last started</div>
                <div className="font-mono text-xs mt-1 text-ink">{formatTime(job.last_started_at)}</div>
              </div>
              <div>
                <div className="text-xs text-ink-dim">Last finished</div>
                <div className="font-mono text-xs mt-1 text-ink">{formatTime(job.last_finished_at)}</div>
              </div>
              <div>
                <div className="text-xs text-ink-dim">Exit code</div>
                <div className="font-mono text-xs mt-1 text-ink">{job.last_exit_code ?? "—"}</div>
              </div>
            </div>

            <button
              onClick={handleTrigger}
              disabled={triggering}
              className="mt-6 rounded-lg bg-brand-blue px-4 py-2 text-sm font-medium text-void hover:bg-brand-blue/90 transition-colors disabled:opacity-50"
            >
              {triggering ? "Triggering..." : "Run now"}
            </button>

            {job.recent_runs.length > 0 && job.recent_runs[0].status === "running" && (
              <div className="mt-8 rounded-xl border border-hairline bg-panel overflow-hidden">
                <div className="border-b border-hairline px-4 py-3">
                  <h2 className="text-xs font-medium tracking-wide text-ink-muted">Live output</h2>
                </div>
                <pre className="max-h-80 overflow-auto whitespace-pre-wrap p-4 font-mono text-xs text-ink">
                  {job.recent_runs[0].stdout_tail || "(no output yet)"}
                </pre>
                {job.recent_runs[0].stderr_tail && (
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap border-t border-status-failed/30 bg-status-failed-bg p-4 font-mono text-xs text-status-failed">
                    {job.recent_runs[0].stderr_tail}
                  </pre>
                )}
              </div>
            )}

            <div className="mt-8 rounded-xl border border-hairline bg-panel overflow-hidden">
              <div className="border-b border-hairline px-4 py-3">
                <h2 className="text-xs font-medium tracking-wide text-ink-muted">Recent runs</h2>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-hairline text-left text-ink-muted">
                      <th className="px-4 py-2 font-medium">Started</th>
                      <th className="px-4 py-2 font-medium">Trigger</th>
                      <th className="px-4 py-2 font-medium">Status</th>
                      <th className="px-4 py-2 font-medium">Exit code</th>
                    </tr>
                  </thead>
                  <tbody>
                    {job.recent_runs.map((run) => (
                      <tr key={run.id} className="border-b border-hairline last:border-0">
                        <td className="px-4 py-2 font-mono text-xs text-ink">{formatTime(run.started_at)}</td>
                        <td className="px-4 py-2 font-mono text-xs text-ink-muted">{run.trigger_source}</td>
                        <td className="px-4 py-2 font-mono text-xs text-ink-muted">{run.status}</td>
                        <td className="px-4 py-2 font-mono text-xs text-ink-muted">{run.exit_code ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </main>
    </>
  );
}