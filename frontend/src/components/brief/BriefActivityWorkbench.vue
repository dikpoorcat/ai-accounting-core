<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { BriefActivityGroup, BriefVoucher } from "../../api/brief";
import { fen, formatFen } from "../../utils/money";

const props = defineProps<{
  groups: BriefActivityGroup[];
  vouchers: BriefVoucher[];
  voucherCount: number;
  lineCount: number;
}>();

const mode = ref<"business" | "voucher">("business");
const selectedBusinessKey = ref("");
const selectedVoucherNumber = ref("");

const selectedBusiness = computed(
  () => props.groups.find((item) => item.key === selectedBusinessKey.value) || null,
);

function resetSelection() {
  mode.value = "business";
  selectedBusinessKey.value = props.groups[0]?.key || "";
  selectedVoucherNumber.value = "";
}

function selectMode(value: "business" | "voucher") {
  if (value === "voucher" && mode.value !== value) selectedVoucherNumber.value = "";
  mode.value = value;
  if (value === "business" && !selectedBusiness.value) {
    selectedBusinessKey.value = props.groups[0]?.key || "";
  }
}

function toggleVoucher(number: string) {
  selectedVoucherNumber.value = selectedVoucherNumber.value === number ? "" : number;
}

function formatDate(value: string) {
  const [, month, day] = value.slice(0, 10).split("-");
  return `${Number(month)} 月 ${Number(day)} 日`;
}

watch(() => [props.groups, props.vouchers], resetSelection, { immediate: true });
</script>

<template>
  <section class="brief-section activity-section" aria-labelledby="activity-title">
    <div class="section-heading">
      <div>
        <p class="section-kicker">业务与凭证</p>
        <h2 id="activity-title">本月发生了什么</h2>
        <p>{{ voucherCount }} 张正式凭证 · {{ groups.length }} 类业务 · {{ lineCount }} 行分录</p>
      </div>
      <div class="view-switch" role="group" aria-label="本月业务查看方式">
        <button
          type="button"
          :aria-pressed="mode === 'business'"
          @click="selectMode('business')"
        >
          按业务看
        </button>
        <button
          type="button"
          :aria-pressed="mode === 'voucher'"
          @click="selectMode('voucher')"
        >
          按凭证看
        </button>
      </div>
    </div>

    <div v-if="mode === 'business' && groups.length" class="workbench">
      <nav class="index" aria-label="业务分类">
        <button
          v-for="group in groups"
          :key="group.key"
          type="button"
          :aria-current="selectedBusinessKey === group.key ? 'true' : undefined"
          @click="selectedBusinessKey = group.key"
        >
          <span>
            <strong>{{ group.label }}</strong>
            <small>{{ group.type_counts.map((item) => `${item.label} ${item.count}`).join(" · ") }}</small>
          </span>
          <b>{{ group.event_count }}</b>
        </button>
      </nav>

      <div v-if="selectedBusiness" class="detail" aria-live="polite">
        <header class="detail-heading">
          <div>
            <span>业务分类</span>
            <h3>{{ selectedBusiness.label }}</h3>
          </div>
          <strong>{{ selectedBusiness.event_count }} 项业务动作</strong>
        </header>
        <ul class="event-list">
          <li
            v-for="item in selectedBusiness.rows"
            :key="`${item.reference}-${item.title}`"
            class="event-row"
          >
            <div class="event-top">
              <div>
                <small>{{ formatDate(item.date) }} · {{ item.reference }}</small>
                <span class="event-type">{{ item.title }}</span>
                <strong class="event-subject">{{ item.subject || item.title }}</strong>
                <span class="event-description">{{ item.description }}</span>
              </div>
              <b>{{ formatFen(item.amount_fen) }}</b>
            </div>
            <div class="event-meta">
              <span :class="['state', { correction: item.state.includes('冲正') }]">
                {{ item.state }}
              </span>
              <span>{{ item.party || "无往来对象" }}</span>
            </div>
            <details v-if="item.evidence.length" class="disclosure">
              <summary>查看 {{ item.evidence.length }} 份关联凭据</summary>
              <ul>
                <li v-for="evidence in item.evidence" :key="evidence">{{ evidence }}</li>
              </ul>
            </details>
          </li>
        </ul>
      </div>
    </div>

    <div v-else-if="mode === 'voucher' && vouchers.length" class="voucher-view">
      <div class="voucher-summary">
        <span>点开凭证，可查看完整摘要、科目和借贷分录。</span>
        <strong>{{ voucherCount }} 张凭证 · {{ lineCount }} 行分录</strong>
      </div>
      <div class="voucher-list">
        <template v-for="(voucher, index) in vouchers" :key="voucher.number">
          <div v-if="index === 0 || vouchers[index - 1]?.date !== voucher.date" class="voucher-date">
            {{ formatDate(voucher.date) }}
          </div>
          <button
            class="voucher-row"
            type="button"
            :aria-expanded="selectedVoucherNumber === voucher.number"
            @click="toggleVoucher(voucher.number)"
          >
            <strong>{{ voucher.number }}</strong>
            <span class="voucher-type">{{ voucher.type }}</span>
            <span class="voucher-row-summary">{{ voucher.list_summary || voucher.summary }}</span>
            <strong class="voucher-row-amount">{{ formatFen(voucher.amount_fen) }}</strong>
            <span class="voucher-toggle">
              {{ selectedVoucherNumber === voucher.number ? "收起" : "展开" }}
            </span>
          </button>

          <section
            v-if="selectedVoucherNumber === voucher.number"
            class="voucher-detail"
            :aria-label="`${voucher.number} 凭证明细`"
          >
            <div class="voucher-detail-top">
              <div class="voucher-description">
                <span class="detail-label">凭证摘要</span>
                <span class="voucher-detail-meta">凭证状态 · {{ voucher.state }}</span>
                <p>{{ voucher.summary }}</p>
              </div>
              <div class="voucher-balance">
                <span>借方合计</span>
                <span>贷方合计</span>
                <strong>{{ formatFen(voucher.amount_fen) }}</strong>
                <strong>{{ formatFen(voucher.amount_fen) }}</strong>
              </div>
            </div>
            <div class="table-wrap">
              <table>
                <colgroup>
                  <col class="voucher-account-column" />
                  <col class="voucher-party-column" />
                  <col class="voucher-amount-column" />
                  <col class="voucher-amount-column" />
                </colgroup>
                <thead>
                  <tr>
                    <th>科目</th>
                    <th>往来对象</th>
                    <th class="number">借方</th>
                    <th class="number">贷方</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="line in voucher.lines" :key="line.line_number">
                    <td data-label="科目">
                      <small>{{ line.code }}</small>
                      <strong>{{ line.account }}</strong>
                    </td>
                    <td data-label="往来对象">
                      <span :class="{ party: line.party }">{{ line.party || "—" }}</span>
                    </td>
                    <td class="number" data-label="借方">
                      {{ fen(line.debit_fen) ? formatFen(line.debit_fen) : "—" }}
                    </td>
                    <td class="number" data-label="贷方">
                      {{ fen(line.credit_fen) ? formatFen(line.credit_fen) : "—" }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <details v-if="voucher.evidence.length" class="disclosure">
              <summary>查看 {{ voucher.evidence.length }} 份关联凭据</summary>
              <ul>
                <li v-for="evidence in voucher.evidence" :key="evidence">{{ evidence }}</li>
              </ul>
            </details>
          </section>
        </template>
      </div>
    </div>

    <p v-else class="empty">
      {{ mode === "business" ? "本月没有正式凭证业务。" : "本月没有凭证。" }}
    </p>
  </section>
</template>

<style scoped>
.brief-section {
  padding: 20px;
  border: 1px solid var(--brief-line);
  border-radius: 20px;
  background: var(--brief-surface);
  box-shadow: var(--brief-shadow);
}

.section-heading,
.detail-heading,
.event-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}

.section-heading {
  align-items: flex-end;
  margin-bottom: 14px;
}

.section-kicker,
.detail-heading > div > span {
  margin: 0 0 4px;
  color: var(--brief-green);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

h2,
h3,
p {
  margin-top: 0;
}

h2 {
  margin-bottom: 3px;
  font-size: 23px;
  letter-spacing: -0.025em;
}

h3 {
  margin-bottom: 0;
}

.section-heading p:last-child,
.voucher-heading p {
  margin-bottom: 0;
  color: var(--brief-muted);
  font-size: 13px;
}

.view-switch {
  display: grid;
  flex: none;
  grid-template-columns: repeat(2, 1fr);
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--brief-line);
  border-radius: 11px;
  background: var(--brief-soft);
}

.view-switch button {
  min-height: 34px;
  padding: 0 13px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--brief-muted);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.view-switch button[aria-pressed="true"] {
  background: var(--brief-surface);
  color: var(--brief-text);
  box-shadow: 0 2px 8px rgb(18 45 31 / 8%);
}

.workbench {
  display: grid;
  grid-template-columns: 270px minmax(0, 1fr);
  min-height: 330px;
  overflow: hidden;
  border: 1px solid var(--brief-line);
  border-radius: 16px;
  background: var(--brief-soft);
}

.index {
  display: flex;
  max-height: 560px;
  flex-direction: column;
  gap: 4px;
  overflow-y: auto;
  padding: 8px;
  border-right: 1px solid var(--brief-line);
}

.index button {
  position: relative;
  display: grid;
  min-height: 66px;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  padding: 10px 11px 10px 13px;
  border: 1px solid transparent;
  border-radius: 11px;
  background: transparent;
  color: var(--brief-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.index button::before {
  position: absolute;
  top: 12px;
  bottom: 12px;
  left: 0;
  width: 3px;
  border-radius: 999px;
  background: transparent;
  content: "";
}

.index button:hover {
  border-color: var(--brief-line-strong);
}

.index button[aria-current="true"] {
  border-color: color-mix(in srgb, var(--brief-green) 20%, var(--brief-line));
  background: var(--brief-green-soft);
}

.index button[aria-current="true"]::before {
  background: var(--brief-green);
}

.index button span,
.index button small {
  display: block;
  min-width: 0;
}

.index button small {
  overflow: hidden;
  margin-top: 4px;
  color: var(--brief-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.index button b {
  color: var(--brief-green);
  font-size: 13px;
  white-space: nowrap;
}

.detail {
  min-width: 0;
  padding: 15px;
  background: var(--brief-surface);
}

.detail-heading {
  padding-bottom: 13px;
  border-bottom: 1px solid var(--brief-line);
}

.detail-heading > strong {
  color: var(--brief-muted);
  font-size: 13px;
}

.event-list,
.disclosure ul {
  display: grid;
  gap: 6px;
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}

.event-row {
  padding: 10px 12px;
  border: 1px solid var(--brief-line);
  border-radius: 12px;
  background: var(--brief-surface);
}

.event-top > div {
  display: grid;
  gap: 3px;
}

.event-top small,
.event-meta {
  color: var(--brief-muted);
  font-size: 12px;
}

.event-type {
  margin-top: 3px;
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 800;
}

.event-subject {
  font-size: 16px;
  line-height: 1.35;
}

.event-description {
  color: var(--brief-muted);
  font-size: 12px;
  line-height: 1.45;
}

.event-top > b {
  font-size: 16px;
  white-space: nowrap;
}

.event-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-top: 8px;
}

.state,
.party {
  display: inline-flex;
  min-height: 23px;
  align-items: center;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--brief-green-soft);
  color: var(--brief-green);
  font-weight: 750;
}

.state.correction {
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.voucher-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 10px;
  color: var(--brief-muted);
  font-size: 12px;
}

.voucher-summary strong {
  color: var(--brief-text);
  white-space: nowrap;
}

.voucher-list {
  display: grid;
  gap: 6px;
}

.voucher-date {
  padding: 8px 12px 2px;
  color: var(--brief-muted);
  font-size: 11px;
  font-weight: 800;
}

.voucher-row {
  display: grid;
  width: 100%;
  min-height: 48px;
  grid-template-columns: 125px 110px minmax(0, 1fr) 130px 42px;
  gap: 12px;
  align-items: center;
  padding: 10px 12px;
  border: 1px solid var(--brief-line);
  border-radius: 11px;
  background: var(--brief-surface);
  color: var(--brief-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.voucher-type {
  color: var(--brief-muted);
  font-size: 12px;
  white-space: nowrap;
}

.voucher-row:hover,
.voucher-row[aria-expanded="true"] {
  border-color: var(--brief-green);
  background: color-mix(in srgb, var(--brief-green-soft) 42%, var(--brief-surface));
}

.voucher-row-summary {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.voucher-row-amount {
  text-align: right;
  white-space: nowrap;
}

.voucher-toggle {
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 750;
  text-align: right;
}

.voucher-detail {
  --voucher-account-width: 35%;
  --voucher-party-width: 33%;
  --voucher-amount-width: 16%;
  margin: -2px 0 8px;
  padding: 16px;
  border: 1px solid var(--brief-green);
  border-radius: 11px;
  background: var(--brief-green-soft);
}

.voucher-detail-top {
  display: grid;
  grid-template-columns:
    var(--voucher-account-width)
    var(--voucher-party-width)
    var(--voucher-amount-width)
    var(--voucher-amount-width);
  gap: 0;
  margin-bottom: 12px;
}

.voucher-description {
  grid-column: 1 / 3;
  padding: 0 9px;
}

.detail-label {
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.06em;
}

.voucher-detail-meta {
  display: block;
  margin-top: 2px;
  color: var(--brief-muted);
  font-size: 11px;
}

.voucher-description p {
  margin: 4px 0 0;
  font-size: 13px;
}

.voucher-balance {
  display: grid;
  grid-column: 3 / 5;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 3px 12px;
  align-content: start;
  text-align: right;
}

.voucher-balance span {
  color: var(--brief-muted);
  font-size: 11px;
}

.voucher-balance strong {
  font-size: 15px;
}

.voucher-account-column {
  width: var(--voucher-account-width);
}

.voucher-party-column {
  width: var(--voucher-party-width);
}

.voucher-amount-column {
  width: var(--voucher-amount-width);
}

.table-wrap {
  overflow-x: auto;
  margin-top: 14px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th,
td {
  padding: 10px 9px;
  border-bottom: 1px solid var(--brief-line);
  text-align: left;
}

th {
  color: var(--brief-muted);
  font-size: 11px;
}

td:first-child {
  display: grid;
  gap: 2px;
}

td:first-child small {
  color: var(--brief-muted);
}

.number {
  text-align: right;
  white-space: nowrap;
}

.disclosure {
  margin-top: 10px;
}

.disclosure summary {
  color: var(--brief-green);
  font-size: 12px;
  font-weight: 750;
  cursor: pointer;
}

.disclosure ul {
  gap: 3px;
  color: var(--brief-muted);
  font-size: 12px;
}

.empty {
  margin: 0;
  padding: 30px;
  border: 1px dashed var(--brief-line-strong);
  border-radius: 14px;
  color: var(--brief-muted);
  text-align: center;
}

@media (max-width: 760px) {
  .brief-section {
    padding: 17px;
    border-radius: 17px;
  }

  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }

  .view-switch {
    width: 100%;
  }

  .view-switch button {
    min-height: 44px;
  }

  .workbench {
    display: block;
  }

  .index {
    max-height: none;
    flex-direction: row;
    overflow-x: auto;
    border-right: 0;
    border-bottom: 1px solid var(--brief-line);
    scroll-snap-type: x proximity;
  }

  .index button {
    min-width: 210px;
    min-height: 64px;
    scroll-snap-align: start;
  }

  .voucher-summary {
    align-items: flex-start;
    flex-direction: column;
    gap: 2px;
  }

  .voucher-row {
    min-height: 72px;
    grid-template-columns: 92px minmax(0, 1fr) auto;
    gap: 5px 9px;
    padding: 10px;
  }

  .voucher-type {
    grid-row: 1;
    grid-column: 2;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .voucher-row-summary {
    display: -webkit-box;
    grid-row: 2;
    grid-column: 1 / 3;
    overflow: hidden;
    white-space: normal;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
  }

  .voucher-row-amount {
    grid-row: 1;
    grid-column: 3;
  }

  .voucher-toggle {
    grid-row: 2;
    grid-column: 3;
  }

  .detail {
    padding: 15px;
  }

  .disclosure summary {
    display: flex;
    min-height: 44px;
    align-items: center;
  }

  .event-top {
    align-items: flex-start;
    flex-direction: column;
  }

  .event-top {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .voucher-detail {
    padding: 14px;
  }

  .voucher-detail-top {
    grid-template-columns: 1fr;
  }

  .voucher-description,
  .voucher-balance {
    grid-column: 1;
    padding: 0;
  }

  .voucher-balance {
    width: min(100%, 300px);
    margin-top: 12px;
    text-align: left;
  }

  table,
  tbody,
  tr,
  td {
    display: block;
    width: 100%;
  }

  thead {
    display: none;
  }

  tr {
    padding: 8px 0;
    border-bottom: 1px solid var(--brief-line);
  }

  td,
  td:first-child {
    display: grid;
    grid-template-columns: 86px minmax(0, 1fr);
    gap: 8px;
    padding: 5px 0;
    border: 0;
    text-align: left;
    white-space: normal;
  }

  td::before {
    color: var(--brief-muted);
    font-size: 11px;
    font-weight: 750;
    content: attr(data-label);
  }

  td:first-child small,
  td:first-child strong {
    grid-column: 2;
  }
}
</style>
