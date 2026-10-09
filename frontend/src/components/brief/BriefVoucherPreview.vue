<script setup lang="ts">
import { nextTick, ref } from "vue";
import type { BriefVoucher } from "../../api/brief";
import { fen, formatFen } from "../../utils/money";
const props = defineProps<{ voucher?: BriefVoucher; active: boolean }>();
const emit = defineEmits<{ preview: [active: boolean]; open: [id: string] }>();
const panel = ref<HTMLElement | null>(null), anchor = ref<HTMLButtonElement | null>(null);
const offset = ref(0), arrow = ref("50%");
let generation = 0;
async function show() {
  if (!props.voucher || window.matchMedia("(max-width: 760px)").matches) return;
  const current = ++generation;
  offset.value = 0; arrow.value = "50%"; emit("preview", true);
  await nextTick();
  if (current !== generation || !props.active || !panel.value || !anchor.value) return;
  const bounds = panel.value.getBoundingClientRect(), button = anchor.value.getBoundingClientRect();
  const top = Math.max(12, Math.min(bounds.top, window.innerHeight - 12 - bounds.height));
  offset.value = top - bounds.top; arrow.value = `${button.top + button.height / 2 - top}px`;
}
function hide() { generation++; emit("preview", false); }
</script>
<template>
  <span class="voucher-link" @mouseenter="show" @mouseleave="hide">
    <button ref="anchor" class="event-voucher-button" type="button" :disabled="!voucher" :aria-describedby="active && voucher ? `preview-${voucher.voucher_version_id}` : undefined" @focus="show" @blur="hide" @click.stop="voucher && (hide(), emit('open', voucher.voucher_version_id))">{{ voucher ? '凭证 ' + voucher.number : '—' }}</button>
    <span v-if="active && voucher" :id="`preview-${voucher.voucher_version_id}`" ref="panel" class="preview event-voucher-preview dashboard-hover-preview" data-side="left" :style="{ '--offset': `${offset}px`, '--preview-arrow': arrow }" role="tooltip">
      <span class="heading"><small>凭证 {{ voucher.number }} · {{ voucher.date || voucher.recognition.label }}</small><strong>{{ voucher.list_summary }}</strong></span>
      <span class="amount"><small>{{ voucher.business_amount_label }}</small><b>{{ formatFen(voucher.business_amount_fen) }}</b></span>
      <span class="lines"><span v-for="line in voucher.lines" :key="line.line_number"><span>{{ line.account }}</span><strong>{{ fen(line.debit_fen) ? '借 ' + formatFen(line.debit_fen) : '贷 ' + formatFen(line.credit_fen) }}</strong></span></span>
      <span class="footer"><span>{{ voucher.state }}</span><small>点击打开凭证详情</small></span>
    </span>
  </span>
</template>
<style scoped>
.voucher-link { position: relative; justify-self: end; }
button { min-height: 32px; padding: 0 9px; border: 0; border-radius: 8px; background: transparent; color: var(--accent); font: inherit; font-size: 11px; font-weight: 750; cursor: pointer; white-space: nowrap; }
button:hover, button:focus-visible { background: var(--accent-soft); }
button:disabled { color: var(--muted); cursor: default; }
.preview { position: absolute; top: 50%; right: calc(100% + 10px); z-index: 30; display: grid; width: min(380px, calc(100vw - 48px)); gap: 10px; pointer-events: none; transform: translateY(calc(-50% + var(--offset))); }
.heading, .amount { display: grid; gap: 3px; }
.heading strong { font-size: 13px; overflow-wrap: anywhere; }
small { color: var(--muted); font-size: 10px; }
.lines { display: grid; gap: 5px; padding: 8px; border-radius: 8px; background: var(--surface-soft); }
.lines > span, .footer { display: flex; justify-content: space-between; gap: 12px; font-size: 11px; }
.lines strong { flex: none; white-space: nowrap; }
@media(max-width:760px) { .preview { display:none; } button { min-height:44px; } }
</style>
