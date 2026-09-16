"use client";

import { useState } from "react";
import { Bot } from "lucide-react";
import { useTelegramStatus } from "@/lib/useTelegramStatus";

interface TestResult {
  ok: boolean;
  result?: Record<string, unknown>;
  error?: string;
}

export default function TelegramPage() {
  const data = useTelegramStatus();
  const [testing, setTesting] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, TestResult>>({});

  async function runTest(botKey: string) {
    setTesting(botKey);
    try {
      const res = await fetch(`/api/telegram-bots/${botKey}/test`, { method: "POST" });
      const body: TestResult = await res.json();
      setTestResults((prev) => ({ ...prev, [botKey]: body }));
    } catch (reason) {
      setTestResults((prev) => ({
        ...prev,
        [botKey]: { ok: false, error: reason instanceof Error ? reason.message : "Test failed" },
      }));
    } finally {
      setTesting(null);
    }
  }

  return (
    <>
      <header className="border-b border-hairline bg-panel">
        <div className="px-6 py-4">
          <h1 className="text-sm font-medium text-ink">Telegram</h1>
        </div>
      </header>

      <main className="flex-1 px-6 py-8">
        <div className="mx-auto max-w-5xl">
          {data === null && <p className="text-sm text-ink-muted">Loading...</p>}

          {data && (
            <>
              <h2 className="mb-2 text-xs font-medium tracking-wide text-ink-muted">Bots</h2>
              <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
                {Object.entries(data.bots).map(([key, bot]) => (
                  <div key={key} className="rounded-xl border border-hairline bg-panel p-4">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <Bot size={16} className="text-ink-muted" />
                        <span className="text-sm text-ink">{bot.label}</span>
                      </div>
                      <span className={`h-1.5 w-1.5 rounded-full ${bot.reachable ? "bg-status-healthy" : "bg-status-failed"}`} />
                    </div>
                    <div className="mt-2 font-mono text-xs text-ink-muted">
                      {bot.reachable ? `@${bot.username}` : bot.error}
                    </div>
                    {bot.reachable && bot.expected_username !== `@${bot.username}` && (
                      <div className="mt-1 text-[11px] text-status-overdue">
                        expected {bot.expected_username}
                      </div>
                    )}

                    <button
                      onClick={() => runTest(key)}
                      disabled={testing === key}
                      className="mt-3 rounded-lg border border-hairline bg-panel-raised px-3 py-1.5 text-xs text-ink hover:border-brand-blue hover:text-brand-blue transition-colors disabled:opacity-50"
                    >
                      {testing === key ? "Testing..." : "Test"}
                    </button>

                    {testResults[key] && (
                      <div className="mt-2 rounded-lg border border-hairline bg-void p-2">
                        {testResults[key].ok ? (
                          <pre className="whitespace-pre-wrap font-mono text-[10px] text-status-healthy">
                            {JSON.stringify(testResults[key].result, null, 2)}
                          </pre>
                        ) : (
                          <p className="font-mono text-[10px] text-status-failed">{testResults[key].error}</p>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>

              <h2 className="mb-2 text-xs font-medium tracking-wide text-ink-muted">Chats &amp; groups</h2>
              <div className="rounded-xl border border-hairline bg-panel overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-hairline text-left text-ink-muted">
                        <th className="px-4 py-3 font-medium">Purpose</th>
                        <th className="px-4 py-3 font-medium">Bot</th>
                        <th className="px-4 py-3 font-medium">Env var</th>
                        <th className="px-4 py-3 font-medium">Title</th>
                        <th className="px-4 py-3 font-medium">Type</th>
                        <th className="px-4 py-3 font-medium">Members</th>
                        <th className="px-4 py-3 font-medium">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.chats.map((chat) => (
                        <tr key={chat.env_var} className="border-b border-hairline last:border-0">
                          <td className="px-4 py-2 text-ink">{chat.label}</td>
                          <td className="px-4 py-2 text-ink-muted">{chat.bot}</td>
                          <td className="px-4 py-2 font-mono text-xs text-ink-dim">{chat.env_var}</td>
                          <td className="px-4 py-2 text-ink-muted">{chat.title ?? "—"}</td>
                          <td className="px-4 py-2 text-ink-muted">{chat.type ?? "—"}</td>
                          <td className="px-4 py-2 text-ink-muted">{chat.member_count ?? "—"}</td>
                          <td className="px-4 py-2">
                            <span
                              className={`h-1.5 w-1.5 rounded-full inline-block ${chat.reachable ? "bg-status-healthy" : "bg-status-failed"}`}
                              title={chat.error ?? undefined}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </div>
      </main>
    </>
  );
}