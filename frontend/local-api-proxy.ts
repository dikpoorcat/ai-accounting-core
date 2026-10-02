import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { request as httpRequest, type OutgoingHttpHeaders, type ServerResponse } from "node:http";
import { resolve } from "node:path";
import { isDeepStrictEqual, promisify } from "node:util";

import type { Plugin } from "vite";

const runFile = promisify(execFile);

export interface LocalServiceMetadata {
  port: number;
  capability: string;
  catalogId: string;
  stateText: string;
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function readLocalServiceMetadata(repositoryRoot: string, dataRoot: string): Promise<LocalServiceMetadata> {
  const statePath = resolve(dataRoot, ".service.json");
  try {
    const stateText = await readFile(statePath, "utf8");
    // Reuse the kernel's read-only catalog, protocol and live-service verification.
    const { stdout } = await runFile(
      resolve(repositoryRoot, ".tmp-kernel-venv/Scripts/python.exe"),
      ["-I", "-X", "utf8", "-m", "ai_accounting.kernel.cli", "--root", dataRoot, "service-info"],
      { cwd: repositoryRoot, encoding: "utf8", timeout: 10_000, windowsHide: true },
    );
    const metadata: unknown = JSON.parse(stdout);
    const privateState: unknown = JSON.parse(stateText);
    if (stateText !== await readFile(statePath, "utf8")
        || !record(metadata) || !record(privateState)
        || typeof metadata.port !== "number"
        || !Number.isInteger(metadata.port) || metadata.port < 1 || metadata.port > 65535
        || typeof metadata.catalog_id !== "string" || !metadata.catalog_id
        || !["protocol", "pid", "port", "catalog_id", "build_id", "database_format"].every(
          key => key in metadata && key in privateState && isDeepStrictEqual(metadata[key], privateState[key]),
        )
        || typeof privateState.capability !== "string" || !privateState.capability) {
      throw new Error("service metadata changed or is invalid");
    }
    return { port: metadata.port, capability: privateState.capability, catalogId: metadata.catalog_id, stateText };
  } catch {
    // Child-process errors can contain private metadata; never forward them to the browser or Vite log.
    throw new Error(`本地会计服务不可用。请先运行 .\\deploy\\windows\\start_accounting.ps1（资料目录：${dataRoot}），再刷新页面。`);
  }
}

function unavailable(response: ServerResponse) {
  if (response.headersSent) {
    response.destroy();
    return;
  }
  if (response.destroyed || response.writableEnded) return;
  response.writeHead(503, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  response.end(JSON.stringify({
    status: "rejected", code: "local_service_unavailable",
    message: "本地会计服务暂时无法连接，请确认服务已启动后刷新页面。",
  }));
}

export async function localApiProxy(
  statePath: string, discover: () => Promise<LocalServiceMetadata>,
): Promise<Plugin> {
  let current = await discover();
  const catalogId = current.catalogId;
  let needsDiscovery = false;
  let discovering: Promise<LocalServiceMetadata> | undefined;

  function reconnect(): Promise<LocalServiceMetadata> {
    if (!discovering) {
      discovering = (async () => {
        needsDiscovery = true;
        const candidate = await discover();
        if (candidate.catalogId !== catalogId) throw new Error("本地会计服务与启动时的资料目录身份不一致。");
        current = candidate;
        needsDiscovery = false;
        return candidate;
      })().finally(() => { discovering = undefined; });
    }
    return discovering;
  }

  async function connection(): Promise<LocalServiceMetadata> {
    if (discovering) return discovering;
    let stateText: string | undefined;
    try { stateText = await readFile(statePath, "utf8"); } catch { /* Restart may briefly remove the state file. */ }
    if (needsDiscovery || stateText !== current.stateText) return reconnect();
    return current;
  }

  return {
    name: "local-accounting-api",
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        if (!/^\/api(?:\/|\?|$)/.test(request.url || "")) return next();
        let metadata: LocalServiceMetadata;
        try { metadata = await connection(); } catch { unavailable(response); return; }
        if (request.destroyed || response.destroyed) return;
        const target = `http://127.0.0.1:${metadata.port}`;
        const headers: OutgoingHttpHeaders = { ...request.headers, host: `127.0.0.1:${metadata.port}` };
        delete headers["x-local-capability"];
        if (headers.origin) headers.origin = target;
        if (request.url?.split("?", 1)[0] === "/api/browser-ticket") {
          headers["x-local-capability"] = metadata.capability;
        }
        let clientDisconnected = false;
        let failed = false;
        function connectionFailed() {
          if (failed || clientDisconnected || response.writableEnded) return;
          failed = true;
          // A failed request may already have executed. Refresh the connection, never replay the request.
          if (current === metadata) {
            needsDiscovery = true;
            void reconnect().catch(() => { /* The next request can try discovery again. */ });
          }
          unavailable(response);
        }
        const upstream = httpRequest({
          hostname: "127.0.0.1", port: metadata.port, method: request.method, path: request.url, headers,
        }, incoming => {
          if (response.destroyed) { incoming.destroy(); return; }
          response.writeHead(incoming.statusCode || 502, incoming.headers);
          incoming.on("error", connectionFailed);
          incoming.pipe(response);
        });
        response.on("close", () => {
          clientDisconnected = !response.writableEnded;
          upstream.destroy();
        });
        request.on("error", () => { clientDisconnected = true; upstream.destroy(); });
        upstream.on("error", connectionFailed);
        request.pipe(upstream);
      });
    },
  };
}
