"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutGrid, MessageSquare, Radio, Smartphone, type LucideIcon } from "lucide-react";
import { usePreloadTelegramStatus } from "@/lib/useTelegramStatus";

interface NavItem {
  label: string;
  href: string | null;
  icon: LucideIcon;
}

// Real, built sections get a real href. Planned-but-not-yet-built
// sections (GreenAPI/forecasting, Telegram bots, WhatsApp scripts) show
// up in the nav now -- per Jay 2026-09-15, all still coming, just not
// built yet -- but stay honestly marked as "soon" rather than linking
// somewhere that doesn't exist.
const NAV_ITEMS: NavItem[] = [
  { label: "Jobs", href: "/", icon: LayoutGrid },
  { label: "Telegram bots", href: "/telegram", icon: MessageSquare },
  { label: "WhatsApp scripts", href: null, icon: Smartphone },
  { label: "Forecasting", href: null, icon: Radio },
];

export default function Sidebar() {
  const pathname = usePathname();
  usePreloadTelegramStatus();

  return (
    <aside className="w-56 shrink-0 border-r border-hairline bg-panel flex flex-col">
      <div className="flex items-center gap-2 px-4 py-4 border-b border-hairline">
        <Image src="/logo-mark.png" alt="" width={28} height={28} className="rounded" />
        <span className="font-mono text-xs font-semibold brand-gradient-text">
          OMNIORCHESTRATOR
        </span>
      </div>

      <nav className="flex-1 px-2 py-4 space-y-1">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;

          if (!item.href) {
            return (
              <div
                key={item.label}
                className="flex items-center justify-between rounded-lg px-3 py-2 text-ink-dim"
              >
                <span className="flex items-center gap-3 text-sm">
                  <Icon size={16} />
                  {item.label}
                </span>
                <span className="rounded bg-panel-raised px-1.5 py-0.5 font-mono text-[10px]">
                  soon
                </span>
              </div>
            );
          }

          const isActive = item.href === "/" ? pathname === "/" || pathname.startsWith("/jobs") : pathname === item.href;

          return (
            <Link
              key={item.label}
              href={item.href}
              className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                isActive ? "bg-panel-raised text-ink" : "text-ink-muted hover:bg-panel-raised hover:text-ink"
              }`}
            >
              <Icon size={16} />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}