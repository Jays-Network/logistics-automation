export type JobStatus =
  | "healthy"
  | "running"
  | "overdue"
  | "failed"
  | "stuck"
  | "never_run"
  | "not_scheduled";

export interface JobHealth {
  job_name: string;
  description: string | null;
  tags: string[];
  cron_schedule: string | null;
  status: JobStatus;
  last_started_at: string | null;
  last_finished_at: string | null;
  last_exit_code: number | null;
  last_trigger_source: string | null;
  next_expected_at: string | null;
  message: string;
}

export interface JobRun {
  id: number;
  trigger_source: string;
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  status: string;
  stdout_tail: string | null;
  stderr_tail: string | null;
  error_message: string | null;
}

export interface JobDetail extends JobHealth {
  recent_runs: JobRun[];
}

export interface SmartsheetLiveCheck {
  reachable: boolean;
  latency_ms: number;
  error: string | null;
}

export interface SmartsheetPublicStatus {
  indicator: "none" | "minor" | "major" | "critical" | "unknown";
  description: string;
}

export interface SmartsheetStatus {
  live: SmartsheetLiveCheck;
  public_status: SmartsheetPublicStatus;
}

export interface ServiceCheck {
  reachable: boolean;
  latency_ms: number;
  error: string | null;
}

export interface ServiceStatus {
  telegram: ServiceCheck;
  greenapi: ServiceCheck & { state: string | null };
}

export interface NetworkHistorySample {
  timestamp: number;
  bytes_sent_per_sec: number;
  bytes_recv_per_sec: number;
}

export interface NetworkIO {
  bytes_sent_per_sec: number;
  bytes_recv_per_sec: number;
  history: NetworkHistorySample[];
}

export interface PostgresStats {
  reachable: boolean;
  connections: number | null;
  db_size_bytes: number | null;
  error: string | null;
  history: { timestamp: number; connections: number }[];
}

export interface GrafanaCheck {
  reachable: boolean;
  latency_ms: number;
  error: string | null;
  history: { timestamp: number; latency_ms: number }[];
}

export interface SystemStatus {
  network: NetworkIO;
  postgres: PostgresStats;
  grafana: GrafanaCheck;
}