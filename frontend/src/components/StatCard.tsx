import type { LucideIcon } from "lucide-react";

interface StatCardProps {
  icon: LucideIcon;
  label: string;
  value: number | string;
  colorClass: string;
  bgClass: string;
}

export default function StatCard({ icon: Icon, label, value, colorClass, bgClass }: StatCardProps) {
  return (
    <div className="rounded-xl border border-hairline bg-panel p-4">
      <div className="flex items-center gap-3">
        <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${bgClass}`}>
          <Icon size={18} className={colorClass} strokeWidth={2} />
        </div>
        <div>
          <div className="text-xs text-ink-muted">{label}</div>
          <div className="font-mono text-xl font-semibold text-ink">{value}</div>
        </div>
      </div>
    </div>
  );
}