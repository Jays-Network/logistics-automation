/**
 * Server-only helper for calling api_gateway.py (the orchestrator's
 * FastAPI backend). This file is ONLY ever imported from Route Handlers
 * (app/api/.../route.ts), which run server-side -- ORCHESTRATOR_API_KEY
 * (no NEXT_PUBLIC_ prefix, so Next.js never bundles it into client-side
 * JS) stays entirely on the server. The browser only ever talks to this
 * Next.js app's own /api/* routes, never directly to api_gateway.py.
 */

const BASE_URL = process.env.ORCHESTRATOR_API_URL;
const API_KEY = process.env.ORCHESTRATOR_API_KEY;

export class OrchestratorConfigError extends Error {}

function requireConfig(): { baseUrl: string; apiKey: string } {
  if (!BASE_URL || !API_KEY) {
    throw new OrchestratorConfigError(
      "ORCHESTRATOR_API_URL and ORCHESTRATOR_API_KEY must be set in the frontend's own .env.local"
    );
  }
  return { baseUrl: BASE_URL, apiKey: API_KEY };
}

export async function orchestratorFetch(
  path: string,
  init?: RequestInit
): Promise<Response> {
  const { baseUrl, apiKey } = requireConfig();
  return fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      ...init?.headers,
      "X-API-Key": apiKey,
    },
    // Never cache -- this is live operational data, not static content.
    cache: "no-store",
  });
}