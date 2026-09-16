"use client";

// Shared, app-wide cache + poller for Telegram status. Started from the
// Sidebar (mounts once at app startup, persists across every page
// navigation in the App Router) so the data is already warm by the time
// someone actually opens the Telegram page -- per Jay 2026-09-16, rather
// than that page starting a fresh ~2-3s fetch from a cold cache on every
// visit.

import { useEffect, useState } from "react";
import type { TelegramStatus } from "./types";

const POLL_MS = 15000;

let cached: TelegramStatus | null = null;
let listeners: Array<(data: TelegramStatus) => void> = [];
let pollStarted = false;

async function fetchOnce() {
  try {
    const res = await fetch("/api/telegram-status");
    if (!res.ok) return;
    const json: TelegramStatus = await res.json();
    cached = json;
    listeners.forEach((listener) => listener(json));
  } catch {
    // Non-fatal -- next poll retries. Whoever's viewing the page keeps
    // showing the last good data rather than an error.
  }
}

function startPolling() {
  if (pollStarted) return;
  pollStarted = true;
  fetchOnce();
  setInterval(fetchOnce, POLL_MS);
}

/** Call this anywhere to both ensure the background poll is running
 * and get the latest cached value, updated live as new polls complete. */
export function useTelegramStatus(): TelegramStatus | null {
  const [data, setData] = useState<TelegramStatus | null>(cached);

  useEffect(() => {
    startPolling();
    const listener = (next: TelegramStatus) => setData(next);
    listeners.push(listener);
    return () => {
      listeners = listeners.filter((l) => l !== listener);
    };
  }, []);

  return data;
}

/** Sidebar calls this on mount purely to kick off the background poll
 * early -- it doesn't need or render the returned value itself. */
export function usePreloadTelegramStatus(): void {
  useEffect(() => {
    startPolling();
  }, []);
}