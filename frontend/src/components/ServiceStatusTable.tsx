interface ServiceRow {
  name: string;
  ok: boolean;
}

export default function ServiceStatusTable({ rows }: { rows: ServiceRow[] }) {
  return (
    <div className="rounded-xl border border-hairline bg-panel overflow-hidden">
      <div className="border-b border-hairline px-4 py-3">
        <h2 className="text-xs font-medium tracking-wide text-ink-muted">Service status</h2>
      </div>
      <div className="grid grid-cols-2 border-b border-hairline px-4 py-2 text-xs text-ink-muted">
        <div>Service</div>
        <div>Status</div>
      </div>
      {rows.map((row) => (
        <div key={row.name} className="grid grid-cols-2 items-center border-b border-hairline px-4 py-3 last:border-0">
          <span className="text-xs text-ink">{row.name}</span>
          <span className={`h-1.5 w-1.5 rounded-full ${row.ok ? "bg-status-healthy" : "bg-status-failed"}`} />
        </div>
      ))}
    </div>
  );
}