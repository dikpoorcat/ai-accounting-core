import { readFileSync } from "node:fs";
import ts from "typescript";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const source = readFileSync(new URL("../../src/utils/dashboardCollections.ts", import.meta.url), "utf8")
  .replace('from "vue"', `from ${JSON.stringify(pathToFileURL(createRequire(import.meta.url).resolve("vue")).href)}`);
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
export const { appendDashboardCollection, prepareDashboardCollections } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
