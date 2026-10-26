// Minimal host nodes used by the real Vue component renderers in Node tests.
export function rendererNode(tag, text = "") {
  return {
    tag, text, props: {}, style: {}, children: [], parent: null,
    ...(tag === "dialog" ? {
      open: false,
      showModal() { this.open = true; },
      close() { this.open = false; },
    } : {}),
  };
}
