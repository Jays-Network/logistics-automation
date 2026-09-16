import type { JobStatus } from "@/lib/types";

const CONFIG: Record<JobStatus, { label: string; dot: string; text: string; bg: string }> = {
  healthy: { label: "Healthy", dot: "bg-status-healthy", text: "text-status-healthy", bg: "bg-status-healthy-bg" },
  running: { label: "Running", dot: "bg-status-running", text: "text-status-running", bg: "bg-status-running-bg" },
  overdue: { label: "Overdue", dot: "bg-status-overdue", text: "text-status-overdue", bg: "bg-status-overdue-bg" },
  failed: { label: "Failed", dot: "bg-status-failed", text: "text-status-failed", bg: "bg-status-failed-bg" },
  stuck: { label: "Stuck", dot: "bg-status-failed", text: "text-status-failed", bg: "bg-status-failed-bg" },
  never_run: { label: "Never run", dot: "bg-status-idle", text: "text-ink-muted", bg: "bg-status-idle-bg" },
  not_scheduled: { label: "Manual", dot: "bg-status-idle", text: "text-ink-muted", bg: "bg-status-idle-bg" },
};

export default function StatusBadge({ status }: { status: JobStatus }) {
  const cfg = CONFIG[status];
  const isLive = status === "running";
  return (
    <span
      className={`inline-flex items-center gap-2 rounded px-2.5 py-1 text-xs font-medium ${cfg.bg} ${cfg.text}`}
    >
      <span className={`relative flex h-1.5 w-1.5 rounded-full ${cfg.dot}`}>
        {isLive && <span className={`absolute inline-flex h-full w-full rounded-full ${cfg.dot} pulse-live`} />}
      </span>
      {cfg.label}
    </span>
  );
}