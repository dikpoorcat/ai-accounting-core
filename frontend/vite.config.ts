import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

import packageMetadata from "./package.json";

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
    const exactTag = gitDescribe(["describe", "--tags", "--exact-match"]);
    if (exactTag) return withVersionPrefix(exactTag);
  } catch {
    // Continue with a commit-specific version when HEAD is not tagged.
  }
  try {
    const description = gitDescribe(["describe", "--tags", "--long", "--always"]);
    if (description) return withVersionPrefix(description);
  } catch {
    // Release archives may not contain Git metadata.
  }
  return withVersionPrefix(packageMetadata.version);
}

export default defineConfig(({ mode }) => ({
  plugins: [vue()],
  define: {
    __APP_VERSION__: JSON.stringify(dashboardVersion()),
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/financial-reports": "http://127.0.0.1:8765",
    },
  },
  build: {
    outDir: mode === "release" ? "../src/ai_accounting/static/dashboard" : "dist",
    emptyOutDir: true,
  },
}));
