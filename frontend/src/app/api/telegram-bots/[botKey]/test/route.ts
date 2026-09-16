import { orchestratorFetch, OrchestratorConfigError } from "@/lib/orchestrator";

export async function POST(
  _request: Request,
  { params }: { params: Promise<{ botKey: string }> }
) {
  const { botKey } = await params;
  try {
    const response = await orchestratorFetch(`/telegram-bots/${encodeURIComponent(botKey)}/test`, {
      method: "POST",
    });
    const body = await response.text();
    return new Response(body, {
      status: response.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (reason) {
    if (reason instanceof OrchestratorConfigError) {
      return Response.json({ error: reason.message }, { status: 500 });
    }
    const message = reason instanceof Error ? reason.message : "Unexpected error";
    return Response.json({ error: message }, { status: 502 });
  }
}