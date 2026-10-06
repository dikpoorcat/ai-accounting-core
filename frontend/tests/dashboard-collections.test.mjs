import assert from "node:assert/strict";
import { test } from "node:test";
import { computed, createRenderer, h, nextTick, ref, shallowReactive, shallowRef } from "vue";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

function renderer() {
  return createRenderer({
    createElement: tag => ({ tag, children: [], text: "" }),
    createText: text => ({ text }), createComment: text => ({ text }),
    insert(node, parent, anchor) {
      if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
      const index = anchor ? parent.children.indexOf(anchor) : -1;
      parent.children.splice(index < 0 ? parent.children.length : index, 0, node); node.parent = parent;
    },
    remove(node) { node.parent?.children.splice(node.parent.children.indexOf(node), 1); },
    setText: (node, text) => { node.text = text; },
    setElementText: (node, text) => { node.text = text; node.children = []; },
    parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp() {},
  });
}

test("100/200/400 records append linearly and render through deep and shallow page state", async () => {
  for (const total of [100, 200, 400]) for (const shallow of [false, true]) {
    let priorReads = 0, writes = 0, incomingReads = 0, existingLength = 20;
    const items = new Proxy(Array.from({ length: 20 }, (_, id) => ({ id })), {
      get(target, key, receiver) { if (/^\d+$/.test(String(key)) && Number(key) < existingLength) priorReads++; return Reflect.get(target, key, receiver); },
      set(target, key, value, receiver) { if (/^\d+$/.test(String(key))) writes++; return Reflect.set(target, key, value, receiver); },
    });
    const initial = { items: shallow ? shallowReactive(items) : items, page: { next_cursor: "20" } };
    const state = shallow ? shallowRef(initial) : ref(initial);
    const rows = computed(() => state.value.items), original = rows.value;
    const root = { children: [] };
    const app = renderer().createApp({ render: () => h("ul", rows.value.map(row => h("li", { key: row.id }, String(row.id)))) });
    app.mount(root);
    try {
      for (let start = 20; start < total; start += 20) {
        const incoming = new Proxy(Array.from({ length: 20 }, (_, index) => ({ id: start + index })), {
          get(target, key, receiver) { if (/^\d+$/.test(String(key))) incomingReads++; return Reflect.get(target, key, receiver); },
        });
        priorReads = 0; existingLength = start;
        state.value = appendDashboardCollection(state.value, { items: incoming, page: { next_cursor: String(start + 20) } });
        assert.equal(priorReads, 0, "appending must not read or copy existing numeric entries");
        assert.equal(rows.value, original);
        await nextTick();
        assert.equal(root.children[0].children.length, start + 20);
        assert.equal(root.children[0].children.at(-1).text, String(start + 19));
        assert.equal(state.value.page.next_cursor, String(start + 20));
      }
      assert.equal(writes, total - 20);
      assert.equal(incomingReads, total - 20);
    } finally { app.unmount(); }
  }
});
