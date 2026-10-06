import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { compileScript, parse } from "@vue/compiler-sfc";
import { createRenderer, h, nextTick } from "vue";
import { createServer, transformWithEsbuild } from "vite";

function renderer() {
  const node = (tag, text = "") => ({ tag, text, props: {}, children: [], parent: null,
    open: false, showModal() { this.open = true; }, close() { this.open = false; } });
  return createRenderer({
    createElement: tag => node(tag), createText: text => node("#text", text),
    createComment: text => node("#comment", text),
    insert(child, parent, anchor = null) {
      if (child.parent) this.remove(child);
      const index = anchor ? parent.children.indexOf(anchor) : -1;
      parent.children.splice(index < 0 ? parent.children.length : index, 0, child);
      child.parent = parent;
    },
    remove(child) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
      child.parent = null;
    },
    setText: (child, text) => { child.text = text; },
    setElementText: (child, text) => { child.text = text; child.children = []; },
    parentNode: child => child.parent,
    nextSibling: child => child.parent?.children[child.parent.children.indexOf(child) + 1] ?? null,
    patchProp: (child, key, _previous, value) => { child.props[key] = value; },
  });
}
function allNodes(root) { return [root, ...root.children.flatMap(allNodes)]; }
function textOf(root) { return root.text + root.children.map(textOf).join(""); }
function findButton(root, text) {
  const result = allNodes(root).find(node => node.tag === "button" && textOf(node) === text);
  assert(result, `missing button: ${text}`);
  return result;
}
async function settle() {
  await new Promise(resolve => setImmediate(resolve));
  await nextTick();
}

async function withPanel(provisioned, run) {
  const saved = { window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch };
  globalThis.window = { location: { origin: "http://dashboard.invalid" } };
  globalThis.document = { body: { style: { overflow: "auto" } } };
  const calls = [];
  let pending;
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/security-request");
    const body = JSON.parse(options.body);
    calls.push(body);
    if (body.operation === "session_status") {
      return new Response(JSON.stringify({ schema_version: 1, catalog_instance_id: "synthetic-catalog",
        provisioned, login_name: provisioned ? "synthetic-owner" : null, active: provisioned, authenticated: false }));
    }
    assert.equal(body.operation, "request");
    return new Promise(resolve => { pending = () => resolve(new Response(JSON.stringify({
      schema_version: 1, request_id: "synthetic-request", catalog_instance_id: "synthetic-catalog",
      kind: body.payload.kind, status: "cancelled", error_code: null, operation_committed: null,
      login_completed: false, recovery_code_acknowledged: false,
    }))); });
  };
  const sourcePath = fileURLToPath(new URL("../src/components/OwnerSessionPanel.vue", import.meta.url));
  const runtimeId = `${sourcePath.replaceAll("\\", "/")}.runtime-test.ts`;
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
    plugins: [{ name: "real-owner-panel-client-render", resolveId(id) {
      if (id === "virtual:owner-panel-runtime") return runtimeId;
    }, async load(id) {
      if (id !== runtimeId) return;
      const { descriptor } = parse(readFileSync(sourcePath, "utf8"), { filename: sourcePath });
      const compiled = compileScript(descriptor, { id: "owner-panel-regression", inlineTemplate: true });
      return (await transformWithEsbuild(compiled.content, runtimeId, { loader: "ts" })).code;
    } }],
  });
  let app;
  try {
    const { default: component } = await server.ssrLoadModule("virtual:owner-panel-runtime");
    const root = { children: [], text: "" };
    app = renderer().createApp({ render: () => h(component, { authenticated: false, expanded: true }) });
    app.mount(root);
    await settle();
    await run({ root, calls, resolveRequest: () => { assert(pending); pending(); pending = undefined; } });
  } finally {
    app?.unmount();
    await server.close();
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
    }
  }
}

test("unprovisioned owner opens native bootstrap without any browser credential input", async () => {
  await withPanel(false, async ({ root, calls, resolveRequest }) => {
    assert.equal(allNodes(root).filter(node => ["input", "textarea", "select"].includes(node.tag)).length, 0);
    const button = findButton(root, "设置负责人");
    assert.equal(Boolean(button.props.disabled), false);
    const request = button.props.onClick();
    await nextTick();
    assert.deepEqual(calls.at(-1), { operation: "request", payload: { kind: "bootstrap_owner" } });
    assert.equal(button.props.disabled, true);
    assert.equal(allNodes(root).filter(node => node.tag === "input").length, 0);
    resolveRequest();
    await request;
    await settle();
    assert.equal(Boolean(findButton(root, "设置负责人").props.disabled), false);
  });
});

test("existing owner security actions also send only their kind and render no secret fields", async () => {
  await withPanel(true, async ({ root, calls, resolveRequest }) => {
    for (const [label, kind] of [["负责人登录", "login"], ["修改密码更换负责人登录密码›", "change_password"],
      ["恢复访问忘记密码时使用恢复码›", "recover"], ["更换恢复码生成新的恢复码›", "replace_recovery_code"]]) {
      assert.equal(allNodes(root).filter(node => ["input", "textarea", "select"].includes(node.tag)).length, 0);
      const request = findButton(root, label).props.onClick();
      await nextTick();
      assert.deepEqual(calls.at(-1), { operation: "request", payload: { kind } });
      resolveRequest();
      await request;
      await settle();
    }
  });
});
