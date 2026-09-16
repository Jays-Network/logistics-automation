"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { CheckCircle2, PlayCircle, AlertTriangle, XCircle, LayoutGrid } from "lucide-react";
import StatusBadge from "@/components/StatusBadge";
import StatCard from "@/components/StatCard";
import MiniServiceCard from "@/components/MiniServiceCard";
import NetworkTrafficCard from "@/components/NetworkTrafficCard";
import PingWorldMap from "@/components/PingWorldMap";
import ServiceStatusTable from "@/components/ServiceStatusTable";
import type { JobHealth, SmartsheetStatus, ServiceStatus, SystemStatus } from "@/lib/types";

const JOBS_POLL_MS = 5000;
const STATUS_POLL_MS = 15000;

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(0)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function useStatusPoll<T>(path: string): T | null {
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const res = await fetch(path);
        if (!res.ok) return;
        const json: T = await res.json();
        if (!cancelled) setData(json);
      } catch {
        // Non-fatal -- these are secondary status cards, not core functionality
      }
    }
    poll();
    const interval = setInterval(poll, STATUS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [path]);
  return data;
}

export default function Dashboard() {
  const [jobs, setJobs] = useState<JobHealth[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const smartsheet = useStatusPoll<SmartsheetStatus>("/api/smartsheet-status");
  const services = useStatusPoll<ServiceStatus>("/api/service-status");
  const system = useStatusPoll<SystemStatus>("/api/system-status");

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const res = await fetch("/api/jobs");
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.error || `Request failed (${res.status})`);
        }
        const data: JobHealth[] = await res.json();
        if (!cancelled) {
          setJobs(data);
          setError(null);
        }
      } catch (reason) {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "Unexpected error");
        }
      }
    }

    poll();
    const interval = setInterval(poll, JOBS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const counts = useMemo(() => {
    if (!jobs) return null;
    return {
      healthy: jobs.filter((j) => j.status === "healthy").length,
      running: jobs.filter((j) => j.status === "running").length,
      overdue: jobs.filter((j) => j.status === "overdue").length,
      failed: jobs.filter((j) => j.status === "failed" || j.status === "stuck").length,
      total: jobs.length,
    };
  }, [jobs]);

  const serviceRows = [
    { name: "Smartsheet", ok: smartsheet?.live.reachable ?? false },
    { name: "Telegram", ok: services?.telegram.reachable ?? false },
    { name: "GreenAPI / WhatsApp", ok: services?.greenapi.reachable ?? false },
  ];

  return (
    <>
      <header className="border-b border-hairline bg-panel">
        <div className="px-6 py-4">
          <h1 className="text-sm font-medium text-ink">Jobs</h1>
        </div>
      </header>

      <main className="flex-1 px-6 py-8">
        <div className="mx-auto max-w-6xl">
          {error && (
            <div className="mb-6 rounded-xl border border-status-failed/40 bg-status-failed-bg px-4 py-3 text-sm text-status-failed">
              {error}
            </div>
          )}

          {counts && (
            <div className="fade-up mb-6 grid grid-cols-2 gap-4 sm:grid-cols-5">
              <StatCard icon={LayoutGrid} label="Total jobs" value={counts.total} colorClass="text-ink" bgClass="bg-panel-raised" />
              <StatCard icon={CheckCircle2} label="Healthy" value={counts.healthy} colorClass="text-status-healthy" bgClass="bg-status-healthy-bg" />
              <StatCard icon={PlayCircle} label="Running" value={counts.running} colorClass="text-status-running" bgClass="bg-status-running-bg" />
              <StatCard icon={AlertTriangle} label="Overdue" value={counts.overdue} colorClass="text-status-overdue" bgClass="bg-status-overdue-bg" />
              <StatCard icon={XCircle} label="Failed" value={counts.failed} colorClass="text-status-failed" bgClass="bg-status-failed-bg" />
            </div>
          )}

          <div className="fade-up mb-6 grid grid-cols-1 gap-4 lg:grid-cols-[1.2fr_1fr]">
            <NetworkTrafficCard history={system?.network.history ?? []} />
            <div className="flex gap-4">
              <MiniServiceCard
                label="Grafana"
                ok={system?.grafana.reachable ?? false}
                detail={system ? (system.grafana.reachable ? `${system.grafana.latency_ms}ms` : system.grafana.error ?? "down") : "…"}
                sparklineValues={system?.grafana.history.map((h) => h.latency_ms)}
                sparklineColor="#4fc3e8"
              />
              <MiniServiceCard
                label="Postgres"
                ok={system?.postgres.reachable ?? false}
                detail={
                  system?.postgres.connections != null
                    ? `${system.postgres.connections} conns · ${formatBytes(system.postgres.db_size_bytes ?? 0)}`
                    : "…"
                }
                sparklineValues={system?.postgres.history.map((h) => h.connections)}
                sparklineColor="#e8a0d8"
              />
            </div>
          </div>

          <div className="fade-up mb-6">
            <PingWorldMap
              nodes={[
                {
                  // Smartsheet HQ: Bellevue, WA, USA -- a real, public fact
                  name: "Smartsheet",
                  coords: [-122.2, 47.6],
                  ok: smartsheet?.live.reachable ?? false,
                  detail: smartsheet ? (smartsheet.live.reachable ? `${smartsheet.live.latency_ms}ms` : "down") : "…",
                  latencyMs: smartsheet?.live.latency_ms ?? 1000,
                },
                {
                  // Telegram's DC2 (Amsterdam) specifically hosts all
                  // official Bot API endpoints -- confirmed by multiple
                  // independent technical sources, per Jay 2026-09-16.
                  // This is the actual serving infrastructure for the
                  // exact call we make, not just a registered-entity guess.
                  name: "Telegram",
                  coords: [4.8952, 52.3702],
                  ok: services?.telegram.reachable ?? false,
                  detail: services ? (services.telegram.reachable ? `${services.telegram.latency_ms}ms` : "down") : "…",
                  latencyMs: services?.telegram.latency_ms ?? 1000,
                },
                {
                  // Green-API LLC's actual registered address, per Jay
                  // 2026-09-16: Astana, Kabanbai Batyr Avenue 47/2,
                  // Kazakhstan -- a real, confirmed location, not a guess.
                  name: "GreenAPI / WhatsApp",
                  coords: [71.45, 51.18],
                  ok: services?.greenapi.reachable ?? false,
                  detail: services ? (services.greenapi.reachable ? (services.greenapi.state ?? `${services.greenapi.latency_ms}ms`) : "down") : "…",
                  latencyMs: services?.greenapi.latency_ms ?? 1000,
                },
              ]}
            />
          </div>

          <div className="fade-up grid grid-cols-1 gap-4 lg:grid-cols-[1.6fr_1fr]">
            <div className="rounded-xl border border-hairline bg-panel overflow-hidden">
              <div className="flex items-center justify-between border-b border-hairline px-4 py-3">
                <h2 className="text-xs font-medium tracking-wide text-ink-muted">All jobs</h2>
                <span className="font-mono text-xs text-ink-dim">refreshes every {JOBS_POLL_MS / 1000}s</span>
              </div>
              {jobs === null && !error && <p className="p-4 text-ink-muted text-sm">Loading...</p>}
              {jobs && (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-hairline text-left text-ink-muted">
                        <th className="px-4 py-3 font-medium">Job</th>
                        <th className="px-4 py-3 font-medium">Status</th>
                        <th className="px-4 py-3 font-medium">Schedule</th>
                        <th className="px-4 py-3 font-medium">Last run</th>
                      </tr>
                    </thead>
                    <tbody>
                      {jobs.map((job) => (
                        <tr
                          key={job.job_name}
                          className="border-b border-hairline last:border-0 transition-colors hover:bg-panel-raised"
                        >
                          <td className="px-4 py-3">
                            <Link
                              href={`/jobs/${encodeURIComponent(job.job_name)}`}
                              className="font-mono text-xs text-ink hover:text-brand-blue transition-colors"
                            >
                              {job.job_name}
                            </Link>
                            {job.tags.length > 0 && (
                              <span className="ml-2 font-mono text-[10px] text-ink-dim">
                                [{job.tags.join(", ")}]
                              </span>
                            )}
                            {job.description && (
                              <div className="text-xs text-ink-muted mt-0.5">{job.description}</div>
                            )}
                          </td>
                          <td className="px-4 py-3">
                            <StatusBadge status={job.status} />
                          </td>
                          <td className="px-4 py-3 font-mono text-xs text-ink-muted">
                            {job.cron_schedule || "manual"}
                          </td>
                          <td className="px-4 py-3 font-mono text-xs text-ink-muted">
                            {formatTime(job.last_started_at)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            <ServiceStatusTable rows={serviceRows} />
          </div>
        </div>
      </main>
    </>
  );
}