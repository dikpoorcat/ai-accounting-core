import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { request as httpRequest, type OutgoingHttpHeaders, type ServerResponse } from "node:http";
import { resolve } from "node:path";
import { promisify } from "node:util";

import type { Plugin } from "vite";

const runFile = promisify(execFile);
const serviceProtocol = "ai-accounting-modular-service/1";

export interface LocalServiceMetadata {
  port: number;
  capability: string;
  catalogId: string;
  stateFingerprint: string;
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function localServiceStatePath(catalogPath: string): string {
  return `${catalogPath}.modular-service`;
}

async function stateFingerprint(statePath: string): Promise<string> {
  return createHash("sha256").update(await readFile(statePath)).digest("hex");
}

export async function readLocalServiceMetadata(repositoryRoot: string, catalogPath: string): Promise<LocalServiceMetadata> {
  const statePath = localServiceStatePath(catalogPath);
  try {
    const fingerprint = await stateFingerprint(statePath);
    // The kernel owns DPAPI decryption, path checks, catalog identity and live protocol verification.
    // Private metadata is consumed only through this Node child-process pipe, never sent to Vite clients.
    const { stdout } = await runFile(
      resolve(repositoryRoot, ".tmp-kernel-venv/Scripts/python.exe"),
      ["-I", "-X", "utf8", "-c", [
        "import json, sys",
        "from ai_accounting_modular.host.runtime._client import discover",
        "print(json.dumps(discover(sys.argv[1])))",
      ].join("\n"), catalogPath],
      { cwd: repositoryRoot, encoding: "utf8", timeout: 10_000, windowsHide: true },
    );
    const metadata: unknown = JSON.parse(stdout);
    if (fingerprint !== await stateFingerprint(statePath)
        || !record(metadata) || metadata.protocol !== serviceProtocol
        || typeof metadata.port !== "number"
        || !Number.isInteger(metadata.port) || metadata.port < 1 || metadata.port > 65535
        || typeof metadata.catalog_id !== "string" || !metadata.catalog_id
        || metadata.catalog_path !== resolve(catalogPath)
        || typeof metadata.capability !== "string" || metadata.capability.length < 32) {
      throw new Error("service metadata changed or is invalid");
    }
    return { port: metadata.port, capability: metadata.capability, catalogId: metadata.catalog_id, stateFingerprint: fingerprint };
  } catch {
    // Child-process errors can contain private metadata; never forward them to the browser or Vite log.
    throw new Error(`本地会计服务不可用。请先启动此目录库的 V3 后台，再刷新页面（目录库：${catalogPath}）。`);
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

function browserTicketUrl(value: unknown, metadata: LocalServiceMetadata): string {
  if (!record(value) || value.protocol !== serviceProtocol || value.catalog_id !== metadata.catalogId
      || !record(value.result) || typeof value.result.url !== "string") throw new Error("invalid ticket envelope");
  const url = new URL(value.result.url);
  if (url.origin !== `http://127.0.0.1:${metadata.port}` || url.pathname !== "/" || url.search
      || url.username || url.password || !/^#ticket=[A-Za-z0-9_-]+$/.test(url.hash)) throw new Error("invalid ticket URL");
  return url.href;
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
    let fingerprint: string | undefined;
    try { fingerprint = await stateFingerprint(statePath); } catch { /* Restart may briefly remove the state file. */ }
    if (needsDiscovery || fingerprint !== current.stateFingerprint) return reconnect();
    return current;
  }

  return {
    name: "local-accounting-api",
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        if (!/^\/api(?:\/|\?|$)/.test(request.url || "")) return next();
        const ticketRequest = request.url?.split("?", 1)[0] === "/api/browser-ticket";
        const frontendOrigin = `http://127.0.0.1:${request.socket.localPort}`;
        if (request.headers.host !== frontendOrigin.slice("http://".length)
            || (request.headers.origin && request.headers.origin !== frontendOrigin)
            || ticketRequest && (request.method !== "POST" || request.headers.origin !== frontendOrigin)) {
          response.writeHead(403, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
          response.end(JSON.stringify({ status: "rejected", code: "invalid_origin", message: "请从本机开发页面重新打开。" }));
          return;
        }
        let metadata: LocalServiceMetadata;
        try { metadata = await connection(); } catch { unavailable(response); return; }
        if (request.destroyed || response.destroyed) return;
        const target = `http://127.0.0.1:${metadata.port}`;
        const headers: OutgoingHttpHeaders = { ...request.headers, host: `127.0.0.1:${metadata.port}` };
        delete headers["x-local-capability"];
        delete headers["x-modular-capability"];
        if (headers.origin) headers.origin = target;
        if (ticketRequest) {
          delete headers.origin;
          delete headers.cookie;
          delete headers["transfer-encoding"];
          headers["content-type"] = "application/json";
          headers["content-length"] = "2";
          headers["x-modular-capability"] = metadata.capability;
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
          hostname: "127.0.0.1", port: metadata.port, method: request.method,
          path: ticketRequest ? "/private/browser-ticket" : request.url, headers,
        }, incoming => {
          if (response.destroyed) { incoming.destroy(); return; }
          incoming.on("error", connectionFailed);
          if (ticketRequest) {
            const chunks: Buffer[] = [];
            let length = 0;
            incoming.on("data", (chunk: Buffer) => {
              length += chunk.length;
              if (length > 64 * 1024) { incoming.destroy(); connectionFailed(); return; }
              chunks.push(chunk);
            });
            incoming.on("end", () => {
              if (failed || response.destroyed) return;
              try {
                if (incoming.statusCode !== 200) throw new Error("ticket request failed");
                const url = browserTicketUrl(JSON.parse(Buffer.concat(chunks).toString("utf8")), metadata);
                response.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
                response.end(JSON.stringify({ url }));
              } catch { connectionFailed(); }
            });
            return;
          }
          response.writeHead(incoming.statusCode || 502, incoming.headers);
          incoming.pipe(response);
        });
        response.on("close", () => {
          clientDisconnected = !response.writableEnded;
          upstream.destroy();
        });
        request.on("error", () => { clientDisconnected = true; upstream.destroy(); });
        upstream.on("error", connectionFailed);
        if (ticketRequest) { request.resume(); upstream.end("{}"); }
        else request.pipe(upstream);
      });
    },
  };
}
