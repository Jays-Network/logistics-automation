"use client";

// Real world map (CC0 public domain, "BlankMap-Equirectangular.svg" via
// Wikimedia Commons), hosted locally at public/world-map.svg -- no
// external runtime fetch, same principle as the earlier topojson fix.
//
// Genuine equirectangular projection (viewBox 0 0 360 180 -- literally
// 1 unit per degree), so simple linear lon/lat math lines up correctly
// with the actual map. The previous version used a Robinson-style OVAL
// projection map with this same linear math, which is why the dots
// were visibly misplaced -- those are two different coordinate systems,
// not just an approximation error.
//
// Two animated pulses per connection, per Jay 2026-09-16: one color for
// the outbound request, a separate color for the return response.
// Duration is driven by the REAL measured latency for that service, not
// a decorative fixed speed -- a genuinely slower service visibly
// animates slower.

interface ServiceNode {
  name: string;
  coords: [number, number]; // [longitude, latitude]
  ok: boolean;
  detail: string;
  latencyMs: number;
}

// This server's approximate location (Johannesburg, ZA) -- a real,
// known location, unlike the illustrative service markers below.
const ORIGIN = { name: "This server", coords: [28.05, -26.2] as [number, number] };

// Matches the real map SVG's own viewBox exactly.
const WIDTH = 360;
const HEIGHT = 180;

function project([lon, lat]: [number, number]): [number, number] {
  const x = lon + 180;
  const y = 90 - lat;
  return [x, y];
}

// Maps a real latency reading to a perceptible animation duration --
// proportional to the real value, clamped to a range that's neither
// too fast to notice nor so slow it feels broken.
function durationFor(latencyMs: number): number {
  return Math.min(3, Math.max(0.6, latencyMs / 400));
}

export default function PingWorldMap({ nodes }: { nodes: ServiceNode[] }) {
  const [originX, originY] = project(ORIGIN.coords);

  return (
    <div className="rounded-xl border border-hairline bg-panel p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-xs font-medium tracking-wide text-ink-muted">Service connectivity</h2>
      </div>

      <div
        className="relative w-full overflow-hidden rounded-lg"
        style={{ aspectRatio: `${WIDTH} / ${HEIGHT}` }}
      >
        <div
          className="absolute inset-0"
          style={{
            backgroundImage: "url('/world-map.svg')",
            backgroundSize: "cover",
            backgroundPosition: "center",
            filter: "invert(1) brightness(0.4)",
          }}
        />

        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="absolute inset-0 h-full w-full">
          {nodes.map((node) => {
            const [x, y] = project(node.coords);
            const dur = durationFor(node.latencyMs);
            const pathId = `path-${node.name.replace(/[^a-zA-Z0-9]/g, "")}`;
            // Trail: several circles on the same path/timing, each
            // started slightly earlier and drawn smaller/fainter, so
            // they visually lag behind the lead dot -- a comet tail
            // instead of a persistent static line.
            const trailSteps = [0, -0.06, -0.12, -0.18, -0.24];
            return (
              <g key={node.name}>
                <path id={pathId} d={`M ${originX} ${originY} L ${x} ${y}`} fill="none" stroke="none" />
                {node.ok && (
                  <>
                    {trailSteps.map((offset, i) => (
                      <circle key={`out-${i}`} r={1.4 - i * 0.22} fill="#4fc3e8" opacity={1 - i * 0.22}>
                        <animateMotion
                          dur={`${dur}s`}
                          begin={`${offset}s`}
                          repeatCount="indefinite"
                          keyPoints="0;1"
                          keyTimes="0;1"
                          calcMode="linear"
                        >
                          <mpath href={`#${pathId}`} />
                        </animateMotion>
                      </circle>
                    ))}
                    {trailSteps.map((offset, i) => (
                      <circle key={`ret-${i}`} r={1.4 - i * 0.22} fill="#e8a0d8" opacity={1 - i * 0.22}>
                        <animateMotion
                          dur={`${dur}s`}
                          begin={`${dur / 2 + offset}s`}
                          repeatCount="indefinite"
                          keyPoints="1;0"
                          keyTimes="0;1"
                          calcMode="linear"
                        >
                          <mpath href={`#${pathId}`} />
                        </animateMotion>
                      </circle>
                    ))}
                  </>
                )}
              </g>
            );
          })}

          <circle cx={originX} cy={originY} r={2.2} fill="#e8a0d8" stroke="#000" strokeWidth={0.4} />

          {nodes.map((node) => {
            const [x, y] = project(node.coords);
            return (
              <circle
                key={node.name}
                cx={x}
                cy={y}
                r={1.8}
                fill={node.ok ? "#4fc3e8" : "#f2545b"}
                stroke="#000"
                strokeWidth={0.4}
              />
            );
          })}
        </svg>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
        <span className="flex items-center gap-1.5 text-ink-muted">
          <span className="h-1.5 w-1.5 rounded-full bg-brand-magenta" />
          {ORIGIN.name}
        </span>
        {nodes.map((node) => (
          <span key={node.name} className="flex items-center gap-1.5 text-ink-muted">
            <span className={`h-1.5 w-1.5 rounded-full ${node.ok ? "bg-status-healthy" : "bg-status-failed"}`} />
            {node.name}: {node.detail}
          </span>
        ))}
      </div>
      <div className="mt-2 flex items-center gap-4 text-[10px] text-ink-dim">
        <span className="flex items-center gap-1.5">
          <span className="h-1 w-1 rounded-full bg-brand-blue" /> outbound
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-1 w-1 rounded-full bg-brand-magenta" /> response
        </span>
      </div>
      <p className="mt-1 text-[10px] text-ink-dim">
      </p>
    </div>
  );
}