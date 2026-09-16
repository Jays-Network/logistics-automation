import type { NetworkHistorySample } from "@/lib/types";

function formatRate(bytesPerSec: number): { value: string; unit: string } {
  const bitsPerSec = bytesPerSec * 8;
  if (bitsPerSec < 1000) return { value: bitsPerSec.toFixed(0), unit: "bps" };
  if (bitsPerSec < 1_000_000) return { value: (bitsPerSec / 1000).toFixed(1), unit: "Kbps" };
  if (bitsPerSec < 1_000_000_000) return { value: (bitsPerSec / 1_000_000).toFixed(1), unit: "Mbps" };
  return { value: (bitsPerSec / 1_000_000_000).toFixed(2), unit: "Gbps" };
}

function buildPath(values: number[], width: number, height: number, max: number): { line: string; area: string } {
  if (values.length === 0) return { line: "", area: "" };
  const stepX = values.length > 1 ? width / (values.length - 1) : 0;
  const points = values.map((v, i) => {
    const x = i * stepX;
    const y = max > 0 ? height - (v / max) * height : height;
    return [x, y];
  });
  const line = points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${width},${height} L0,${height} Z`;
  return { line, area };
}

export default function NetworkTrafficCard({ history }: { history: NetworkHistorySample[] }) {
  const recvValues = history.map((h) => h.bytes_recv_per_sec);
  const sentValues = history.map((h) => h.bytes_sent_per_sec);
  const maxValue = Math.max(1, ...recvValues, ...sentValues);
  const width = 320;
  const height = 120;

  const recvPath = buildPath(recvValues, width, height, maxValue);
  const sentPath = buildPath(sentValues, width, height, maxValue);

  const latestRecv = recvValues[recvValues.length - 1] ?? 0;
  const latestSent = sentValues[sentValues.length - 1] ?? 0;
  const display = formatRate(latestRecv + latestSent);

  return (
    <div className="rounded-xl border border-hairline bg-panel p-5">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs font-medium tracking-wide text-ink-muted">Network traffic</span>
        <span className="rounded border border-hairline px-2 py-0.5 text-[10px] text-ink-muted">this LXC</span>
      </div>

      <div className="mb-1 flex items-baseline gap-2">
        <span className="font-mono text-2xl font-semibold text-ink">{display.value}</span>
        <span className="text-sm text-ink-muted">{display.unit}</span>
      </div>

      {history.length < 2 ? (
        <p className="mt-8 text-xs text-ink-dim">Collecting samples...</p>
      ) : (
        <>
          <svg viewBox={`0 0 ${width} ${height}`} className="mt-4 block w-full" style={{ height }}>
            <defs>
              <linearGradient id="recvGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#4fc3e8" stopOpacity={0.35} />
                <stop offset="100%" stopColor="#4fc3e8" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="sentGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#e8a0d8" stopOpacity={0.3} />
                <stop offset="100%" stopColor="#e8a0d8" stopOpacity={0} />
              </linearGradient>
            </defs>
            {[0, 0.25, 0.5, 0.75, 1].map((f) => (
              <line key={f} x1={0} y1={height * f} x2={width} y2={height * f} stroke="#1f1f1f" strokeWidth={1} />
            ))}
            <path d={recvPath.area} fill="url(#recvGrad)" />
            <path d={recvPath.line} fill="none" stroke="#4fc3e8" strokeWidth={1.5} />
            <path d={sentPath.area} fill="url(#sentGrad)" />
            <path d={sentPath.line} fill="none" stroke="#e8a0d8" strokeWidth={1.5} />
          </svg>
          <div className="mt-2 flex gap-4">
            <span className="flex items-center gap-1.5 text-[11px] text-ink-muted">
              <span className="h-0.5 w-3 bg-brand-blue" /> Inbound
            </span>
            <span className="flex items-center gap-1.5 text-[11px] text-ink-muted">
              <span className="h-0.5 w-3 bg-brand-magenta" /> Outbound
            </span>
          </div>
        </>
      )}
    </div>
  );
}