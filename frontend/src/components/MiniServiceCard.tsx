import Sparkline from "./Sparkline";

export default function MiniServiceCard({
  label,
  ok,
  detail,
  sparklineValues,
  sparklineColor = "#4fc3e8",
}: {
  label: string;
  ok: boolean;
  detail: string;
  sparklineValues?: number[];
  sparklineColor?: string;
}) {
  return (
    <div className="flex flex-1 flex-col justify-between gap-2 rounded-xl border border-hairline bg-panel p-4 min-h-[104px]">
      <div className="flex items-center justify-between">
        <span className="text-sm text-ink-muted">{label}</span>
        <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-status-healthy" : "bg-status-failed"}`} />
      </div>
      {sparklineValues && <Sparkline values={sparklineValues} color={sparklineColor} />}
      <div className="font-mono text-xs text-ink-dim">{detail}</div>
    </div>
  );
}