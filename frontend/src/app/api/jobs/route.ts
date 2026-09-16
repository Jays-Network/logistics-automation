import { orchestratorFetch, OrchestratorConfigError } from "@/lib/orchestrator";

export async function GET() {
  try {
    const response = await orchestratorFetch("/jobs");
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