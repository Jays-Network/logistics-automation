export default function Sparkline({ values, color }: { values: number[]; color: string }) {
  if (values.length < 2) {
    return <div className="h-8 text-[10px] text-ink-dim">collecting...</div>;
  }
  const width = 200;
  const height = 32;
  const max = Math.max(1, ...values);
  const min = Math.min(0, ...values);
  const range = max - min || 1;
  const stepX = width / (values.length - 1);
  const points = values
    .map((v, i) => `${(i * stepX).toFixed(1)},${(height - ((v - min) / range) * height).toFixed(1)}`)
    .join(" ");

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-8 w-full">
      <polyline points={points} fill="none" stroke={color} strokeWidth={1.5} />
    </svg>
  );
}