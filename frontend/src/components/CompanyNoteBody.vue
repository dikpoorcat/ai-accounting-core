<script lang="ts">
import MarkdownIt, { type Token } from "markdown-it";
import { defineComponent, h } from "vue";
import type { PropType, VNodeChild } from "vue";

const parser = new MarkdownIt({ html: false, linkify: false, typographer: false });
parser.disable("strikethrough");

function safeLink(value: string | number | null): string | null {
  if (typeof value !== "string" || !/^https?:\/\//i.test(value) || /[\u0000-\u0020\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch { return null; }
}

function container(token: Token, children: VNodeChild[]): VNodeChild {
  switch (token.type) {
    case "paragraph_open": return h("p", children);
    case "heading_open":
      switch (token.tag) {
        case "h1": return h("h2", children);
        case "h2": return h("h3", children);
        case "h3": return h("h4", children);
        case "h4": return h("h5", children);
        default: return h("h6", children);
      }
    case "bullet_list_open": return h("ul", children);
    case "ordered_list_open": {
      const raw = token.attrGet("start"), start = raw === null ? 1 : Number(raw);
      return h("ol", Number.isSafeInteger(start) && start >= 0 ? { start } : {}, children);
    }
    case "list_item_open": return h("li", children);
    case "strong_open": return h("strong", children);
    case "blockquote_open": return h("blockquote", children);
    case "table_open": return h("table", { tabindex: 0, "aria-label": "公司说明表格" }, children);
    case "thead_open": return h("thead", children);
    case "tbody_open": return h("tbody", children);
    case "tr_open": return h("tr", children);
    case "th_open": return h("th", alignment(token), children);
    case "td_open": return h("td", alignment(token), children);
    case "link_open": {
      const href = safeLink(token.attrGet("href"));
      return href ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, children) : children;
    }
    // Unsupported emphasis remains readable, including its original delimiters.
    default: return [token.markup, ...children, token.markup];
  }
}

function alignment(token: Token): { class?: string } {
  switch (token.attrGet("style")) {
    case "text-align:left": return { class: "align-left" };
    case "text-align:center": return { class: "align-center" };
    case "text-align:right": return { class: "align-right" };
    default: return {};
  }
}

function renderTokens(tokens: Token[]): VNodeChild[] {
  const output: VNodeChild[] = [];
  const stack: { token: Token; children: VNodeChild[] }[] = [];
  const append = (node: VNodeChild) => (stack.at(-1)?.children ?? output).push(node);
  for (const token of tokens) {
    if (token.nesting === 1) { stack.push({ token, children: [] }); continue; }
    if (token.nesting === -1) {
      const frame = stack.pop();
      if (frame) append(container(frame.token, frame.children));
      continue;
    }
    switch (token.type) {
      case "inline": append(renderTokens(token.children ?? [])); break;
      case "softbreak": append("\n"); break;
      case "hardbreak": append(h("br")); break;
      case "hr": append(h("p", { class: "paragraph-space", "aria-hidden": true }, "\n")); break;
      case "image": append(`图片：${token.content || "未填写说明"}（${token.attrGet("src") ?? ""}）`); break;
      case "code_inline": append(`${token.markup}${token.content}${token.markup}`); break;
      case "fence": append(h("p", `${token.markup}${token.info}\n${token.content}${token.markup}`)); break;
      case "code_block": append(h("p", token.content)); break;
      default: append(token.content);
    }
  }
  while (stack.length) {
    const frame = stack.pop()!;
    append(container(frame.token, frame.children));
  }
  return output;
}

export default defineComponent({
  props: {
    text: { type: String, required: true },
    format: { type: String as PropType<"plain_text" | "markdown">, required: true },
  },
  setup(props) {
    return () => h("article", { class: ["company-note-body", { "plain-text": props.format === "plain_text" }] },
      props.format === "plain_text" ? props.text : renderTokens(parser.parse(props.text, {})));
  },
});
</script>

<style scoped>
.company-note-body { min-width: 0; font-size: 16px; line-height: 1.85; overflow-wrap: anywhere; }
.plain-text, .company-note-body :deep(p) { white-space: pre-wrap; }
.company-note-body :deep(h2), .company-note-body :deep(h3), .company-note-body :deep(h4), .company-note-body :deep(h5), .company-note-body :deep(h6) { margin: 1.8em 0 .65em; line-height: 1.45; color: var(--text); }
.company-note-body :deep(h2) { font-size: 1.35em; }
.company-note-body :deep(h3) { font-size: 1.16em; }
.company-note-body :deep(h4), .company-note-body :deep(h5), .company-note-body :deep(h6) { font-size: 1em; }
.company-note-body :deep(> :first-child) { margin-top: 0; }
.company-note-body :deep(p) { margin: .7em 0; }
.company-note-body :deep(ul), .company-note-body :deep(ol) { padding-inline-start: 1.6em; }
.company-note-body :deep(li) { margin: .3em 0; }
.company-note-body :deep(li > p) { margin: .25em 0; }
.company-note-body :deep(blockquote) { margin: 1em 0; padding: .25em 1em; border-left: 3px solid var(--accent); background: var(--surface-soft); color: var(--muted); }
.company-note-body :deep(table) { display: block; max-width: 100%; overflow-x: auto; margin: 1.2em 0; border-collapse: collapse; font-size: .94em; }
.company-note-body :deep(th), .company-note-body :deep(td) { min-width: 7em; padding: .6em .85em; border: 1px solid var(--line); text-align: left; vertical-align: top; }
.company-note-body :deep(th) { background: var(--surface-soft); }
.company-note-body :deep(.align-left) { text-align: left; }
.company-note-body :deep(.align-center) { text-align: center; }
.company-note-body :deep(.align-right) { text-align: right; }
.company-note-body :deep(a) { color: var(--accent); text-underline-offset: 3px; }
.company-note-body :deep(a:focus-visible), .company-note-body :deep(table:focus-visible) { outline: 2px solid var(--focus); outline-offset: 3px; }
.company-note-body :deep(.paragraph-space) { min-height: .7em; }
</style>
