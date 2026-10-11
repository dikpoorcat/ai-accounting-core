import { readFileSync } from "node:fs";
import ts from "typescript";

const source = readFileSync(new URL("../../src/utils/briefCategoryIndex.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
export const { briefCategoryIndex } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
