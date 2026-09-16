interface ServiceCardProps {
  label: string;
  ok: boolean;
  primary: string;
  secondary?: string;
}

export default function ServiceCard({ label, ok, primary, secondary }: ServiceCardProps) {
  return (
    <div className="rounded-xl border border-hairline bg-panel p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs text-ink-muted">{label}</span>
        <span
          className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-status-healthy" : "bg-status-failed"}`}
        />
      </div>
      <div className="mt-2 font-mono text-lg font-semibold text-ink">{primary}</div>
      {secondary && <div className="mt-0.5 truncate text-[11px] text-ink-dim" title={secondary}>{secondary}</div>}
    </div>
  );
}