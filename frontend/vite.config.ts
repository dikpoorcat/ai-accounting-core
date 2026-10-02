import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

import packageMetadata from "./package.json";
import { localApiProxy, readLocalServiceMetadata } from "./local-api-proxy";

const repositoryRoot = fileURLToPath(new URL("..", import.meta.url));

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

export default defineConfig(async ({ command, mode }) => {
  const dataRoot = resolve(repositoryRoot, process.env.FINANCE_DATA_ROOT || "data/kernel-released");
  const api = command === "serve" ? await localApiProxy(
    resolve(dataRoot, ".service.json"), () => readLocalServiceMetadata(repositoryRoot, dataRoot),
  ) : undefined;
  return {
    plugins: [vue(), api],
    define: {
      __APP_VERSION__: JSON.stringify(dashboardVersion()),
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
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
