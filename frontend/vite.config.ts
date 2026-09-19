import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, type ProxyOptions } from "vite";
import vue from "@vitejs/plugin-vue";

import packageMetadata from "./package.json";

const repositoryRoot = fileURLToPath(new URL("..", import.meta.url));

interface LocalServiceMetadata {
  port: number;
  capability: string;
}

function localServiceMetadata(): LocalServiceMetadata {
  const dataRoot = resolve(repositoryRoot, process.env.FINANCE_DATA_ROOT || "data/kernel-draft");
  const statePath = resolve(dataRoot, ".service.json");
  let metadata: unknown;
  try {
    // The kernel verifies the catalog and exact protocol before exposing a local capability.
    metadata = JSON.parse(execFileSync(
      resolve(repositoryRoot, ".tmp-kernel-venv/Scripts/python.exe"),
      ["-I", "-X", "utf8", "-m", "ai_accounting.kernel.cli", "--root", dataRoot, "service-info"],
      { cwd: repositoryRoot, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
    ));
  } catch {
    throw new Error(
      `本地会计服务未启动。请先在仓库根目录运行 .\\deploy\\windows\\start_accounting.ps1（资料目录：${dataRoot}）。`,
    );
  }
  if (typeof metadata !== "object" || metadata === null
      || !("port" in metadata) || typeof metadata.port !== "number"
      || !Number.isInteger(metadata.port) || metadata.port < 1 || metadata.port > 65535
      || !("capability" in metadata) || typeof metadata.capability !== "string" || !metadata.capability) {
    throw new Error(`本地会计服务状态无效：${statePath}`);
  }
  return { port: metadata.port, capability: metadata.capability };
}

function localApiProxy(metadata: LocalServiceMetadata): ProxyOptions {
  const target = `http://127.0.0.1:${metadata.port}`;
  return {
    target,
    changeOrigin: true,
    configure(proxy) {
      proxy.on("proxyReq", (proxyRequest, request) => {
        if (request.headers.origin) proxyRequest.setHeader("Origin", target);
        if (request.url?.split("?", 1)[0] === "/api/browser-ticket") {
          proxyRequest.setHeader("X-Local-Capability", metadata.capability);
        }
      });
    },
  };
}

function gitDescribe(args: string[]) {
  return execFileSync("git", args, {
    cwd: repositoryRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  }).trim();
}

function withVersionPrefix(version: string) {
  return version.startsWith("v") ? version : `v${version}`;
}

function dashboardVersion() {
  try {
    const description = gitDescribe(["describe", "--tags", "--always", "--dirty"]);
    if (description) return withVersionPrefix(description);
  } catch {
    // Release archives may not contain Git metadata.
  }
  return withVersionPrefix(packageMetadata.version);
}

export default defineConfig(({ command, mode }) => {
  const proxy = command === "serve" ? { "/api": localApiProxy(localServiceMetadata()) } : undefined;
  return {
    plugins: [vue()],
    define: {
      __APP_VERSION__: JSON.stringify(dashboardVersion()),
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy,
    },
    build: {
      outDir: mode === "release" ? "../src/ai_accounting/static/dashboard" : "dist",
      emptyOutDir: true,
      rollupOptions: {
        input: {
          dashboard: fileURLToPath(new URL("./index.html", import.meta.url)),
          local: fileURLToPath(new URL("./local.html", import.meta.url)),
        },
      },
    },
  };
});
