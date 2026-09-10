// The MCP endpoint. This is the URL that goes in Claude's connector settings.
//
// Streamable HTTP, which for a server with no server-initiated messages means:
// POST a JSON-RPC request, get a JSON-RPC response. No SSE stream is opened,
// so GET returns 405 - that is a complete implementation of the transport for
// a read-only tool server, not a gap.
//
// Authentication is mandatory and fails closed. If MCP_ACCESS_PASSWORD is
// unset the endpoint refuses every request rather than serving sales figures
// to the open internet, because a misconfigured deploy must not be an open
// one.

import { bearerFrom, originOf, passwordConfigured, verifyAccessToken } from "./_lib/oauth.js";
import { callTool, TOOLS, ToolError } from "./_lib/tools.js";
import { missingEnv } from "./_lib/db.js";

export const config = { runtime: "edge" };

const PROTOCOL_VERSION = "2025-06-18";
const SUPPORTED_PROTOCOLS = new Set([PROTOCOL_VERSION, "2025-03-26", "2024-11-05"]);

const SERVER_INFO = {
  name: "silverpot-dashboard",
  title: "Silverpot Tea sales and advertising",
  version: "1.0.0",
};

interface RpcRequest {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: Record<string, unknown>;
}

function rpcError(
  id: string | number | null,
  code: number,
  message: string,
): Response {
  return json({ jsonrpc: "2.0", id, error: { code, message } });
}

function json(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/** 401 in the shape the MCP auth spec wants: it names where to authenticate. */
function unauthorized(request: Request): Response {
  const metadata = `${originOf(request)}/.well-known/oauth-protected-resource`;
  return json(
    { error: "invalid_token", error_description: "A valid bearer token is required." },
    401,
    { "WWW-Authenticate": `Bearer resource_metadata="${metadata}"` },
  );
}

export default async function handler(request: Request): Promise<Response> {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, Mcp-Session-Id, MCP-Protocol-Version",
      },
    });
  }

  if (request.method !== "POST") {
    return json({ error: "Use POST. This server opens no SSE stream." }, 405);
  }

  // Fail closed. An unconfigured deploy serves nothing.
  const missing = [...missingEnv(), ...(passwordConfigured() ? [] : ["MCP_ACCESS_PASSWORD"])];
  if (missing.length > 0) {
    return json(
      { error: "server_not_configured", missing },
      503,
    );
  }

  const token = bearerFrom(request);
  if (!token) return unauthorized(request);
  const clientId = await verifyAccessToken(token).catch(() => null);
  if (!clientId) return unauthorized(request);

  let body: RpcRequest | RpcRequest[];
  try {
    body = (await request.json()) as RpcRequest | RpcRequest[];
  } catch {
    return rpcError(null, -32700, "Parse error");
  }

  // A batch is legal JSON-RPC. Everything here is independent, so answering
  // them in order is correct and keeps one slow tool from reordering others.
  if (Array.isArray(body)) {
    const answers = [];
    for (const one of body) {
      const answer = await dispatch(one);
      if (answer) answers.push(answer);
    }
    return answers.length > 0 ? json(answers) : new Response(null, { status: 202 });
  }

  const answer = await dispatch(body);
  // A notification carries no id and gets no response body, only an ack.
  return answer ? json(answer) : new Response(null, { status: 202 });
}

async function dispatch(
  req: RpcRequest,
): Promise<Record<string, unknown> | null> {
  const id = req.id ?? null;
  const isNotification = req.id === undefined || req.id === null;
  const method = req.method ?? "";

  const reply = (result: unknown) => ({ jsonrpc: "2.0", id, result });
  const fail = (code: number, message: string) => ({
    jsonrpc: "2.0",
    id,
    error: { code, message },
  });

  switch (method) {
    case "initialize": {
      const asked = String(req.params?.["protocolVersion"] ?? PROTOCOL_VERSION);
      return reply({
        protocolVersion: SUPPORTED_PROTOCOLS.has(asked) ? asked : PROTOCOL_VERSION,
        capabilities: { tools: { listChanged: false } },
        serverInfo: SERVER_INFO,
        instructions:
          "Silverpot Tea's marketplace figures. Sales are live for Amazon and " +
          "Walmart. Advertising rows may be empty: Amazon Ads is pending API " +
          "approval and Walmart advertising has no available API route. When a " +
          "figure is zero, call data_freshness before reporting it as no sales.",
      });
    }

    case "notifications/initialized":
    case "notifications/cancelled":
      return null;

    case "ping":
      return isNotification ? null : reply({});

    case "tools/list":
      return reply({ tools: TOOLS });

    case "tools/call": {
      const name = String(req.params?.["name"] ?? "");
      const args = (req.params?.["arguments"] ?? {}) as Record<string, unknown>;
      try {
        const result = await callTool(name, args);
        return reply({
          content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
          isError: false,
        });
      } catch (err) {
        // A bad argument is the model's to fix, so it comes back as a tool
        // result it can read and retry. Anything else is ours, and its detail
        // stays in the Vercel log rather than travelling to the client -
        // Postgres error text names columns and constraints.
        if (err instanceof ToolError) {
          return reply({
            content: [{ type: "text", text: `Invalid arguments: ${err.message}` }],
            isError: true,
          });
        }
        console.error("tool failed", name, err);
        return reply({
          content: [{ type: "text", text: "The dashboard database did not answer." }],
          isError: true,
        });
      }
    }

    default:
      return isNotification ? null : fail(-32601, `Method not found: ${method}`);
  }
}
