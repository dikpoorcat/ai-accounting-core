import { readFileSync } from "node:fs";
import ts from "typescript";

// Import the production implementation into script-only view harnesses. Its only
// dependency is a type import, which TypeScript removes during transpilation.
const { outputText } = ts.transpileModule(
  readFileSync(new URL("../../src/utils/briefOpenItems.ts", import.meta.url), "utf8"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } },
);
const implementation = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
export const groupContributionMembers = implementation.groupContributionMembers;
export const contributionProgressRows = implementation.contributionProgressRows;
