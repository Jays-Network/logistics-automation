"use client";

import { useEffect, useState } from "react";
import type { SmartsheetStatus } from "@/lib/types";

const POLL_INTERVAL_MS = 15000;

const INDICATOR_STYLE: Record<string, string> = {
  none: "bg-status-healthy",
  minor: "bg-status-overdue",
  major: "bg-status-failed",
  critical: "bg-status-failed",
  unknown: "bg-status-idle",
};

export default function SmartsheetStatusIndicator() {
  const [status, setStatus] = useState<SmartsheetStatus | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const res = await fetch("/api/smartsheet-status");
        if (!res.ok) return;
        const data: SmartsheetStatus = await res.json();
        if (!cancelled) setStatus(data);
      } catch {
        // Silent -- this is a secondary indicator, not worth an error banner
      }
    }

    poll();
    const interval = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  if (!status) {
    return <span className="text-xs text-ink-dim font-mono">smartsheet: checking...</span>;
  }

  const dotColor = status.live.reachable
    ? INDICATOR_STYLE[status.public_status.indicator] ?? INDICATOR_STYLE.unknown
    : INDICATOR_STYLE.critical;

  const label = !status.live.reachable
    ? "unreachable"
    : status.public_status.indicator === "none"
      ? `ok · ${status.live.latency_ms}ms`
      : status.public_status.description.toLowerCase();

  return (
    <div
      className="flex items-center gap-2 text-xs font-mono text-ink-muted"
      title={
        status.live.reachable
          ? `Our connection: OK (${status.live.latency_ms}ms). Smartsheet reports: ${status.public_status.description}`
          : `Our connection failed: ${status.live.error}`
      }
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dotColor}`} />
      smartsheet: {label}
    </div>
  );
}