<script setup lang="ts">
import { computed } from "vue";

import type { BriefOpenItems } from "../../api/brief";
import { formatFen } from "../../utils/money";

const props = defineProps<{
  openItems: BriefOpenItems;
  periodLabel: string;
  periodStatus: string;
}>();

const isClosed = computed(() => props.periodStatus === "closed");

function categoryLabel(label: string) {
  if (!isClosed.value) return label;
  return label.replace(/^待收回/, "应收").replace(/^待收/, "应收").replace(/^待付/, "应付");
}

function openStateLabel(direction: "receivable" | "payable", status: string) {
  if (direction === "receivable") {
    if (isClosed.value) return status === "partial" ? "关账时部分收回" : "关账时未收回";
    return status === "partial" ? "部分收回" : "待收回";
  }
  if (isClosed.value) return status === "partial" ? "关账时部分支付" : "关账时未支付";
  return status === "partial" ? "部分支付" : "尚未支付";
}

function groupStateLabel(direction: "receivable" | "payable", openCount: number, partialCount: number) {
  if (direction === "receivable") {
    return isClosed.value
      ? `${openCount} 项关账时未收回 · ${partialCount} 项关账时部分收回`
      : `${openCount} 项待收回 · ${partialCount} 项部分收回`;
  }
  return isClosed.value
    ? `${partialCount} 项关账时部分支付 · ${openCount} 项关账时未支付`
    : `${partialCount} 项部分支付 · ${openCount} 项尚未支付`;
}
</script>

<template>
  <section class="brief-section open-items" aria-labelledby="open-items-title">
    <div class="section-heading">
      <div>
        <p class="section-kicker">
          {{ isClosed ? "已关账历史快照 · 不代表当前尚未结算" : "应收与应付分开，不互相抵销" }}
        </p>
        <h2 id="open-items-title">
          {{ isClosed ? `${periodLabel}关账时点往来余额` : "期末往来事项" }}
        </h2>
      </div>
      <strong>
        {{ isClosed ? `${openItems.total_count} 项关账时有余额` : `${openItems.total_count} 项未完全结清` }}
      </strong>
    </div>

    <p v-if="isClosed" class="historical-note">
      以下仅反映该月关账时点的应收、应付余额；若要判断现在是否仍未结算，请查看最新期间。
    </p>

    <div class="open-summary" :aria-label="isClosed ? '关账时点往来汇总' : '期末往来汇总'">
      <article class="receivable">
        <span>{{ isClosed ? "关账时点应收" : "期末待收" }}</span>
        <strong>{{ formatFen(openItems.receivable_fen) }}</strong>
        <small>{{ openItems.receivable_count }} 项</small>
      </article>
      <article class="payable">
        <span>{{ isClosed ? "关账时点应付" : "期末待付" }}</span>
        <strong>{{ formatFen(openItems.payable_fen) }}</strong>
        <small>{{ openItems.payable_count }} 项</small>
      </article>
    </div>

    <div class="categories">
      <details
        v-for="category in openItems.categories.filter((item) => item.count)"
        :key="category.key"
        class="category"
      >
        <summary>
          <span class="category-name">
            <strong>{{ categoryLabel(category.label) }}</strong>
            <small>
              主要往来方：{{ category.groups.slice(0, 2).map((group) => group.party).join("、") || "—" }}
            </small>
          </span>
          <span class="category-total">
            <small>{{ category.count }} {{ category.unit }}</small>
            <strong>{{ formatFen(category.outstanding_fen) }}</strong>
          </span>
          <span class="chevron" aria-hidden="true">⌄</span>
        </summary>
        <div class="category-detail">
          <div class="party-groups">
            <article v-for="group in category.groups" :key="group.party">
              <span>{{ group.party }}</span>
              <strong>{{ formatFen(group.outstanding_fen) }}</strong>
              <small>
                {{ groupStateLabel(category.direction, group.open_count, group.partial_count) }}
              </small>
            </article>
          </div>
          <div class="table-wrap">
            <table>
              <colgroup>
                <col class="voucher-column" />
                <col class="party-column" />
                <col class="description-column" />
                <col class="status-column" />
                <col class="amount-column" />
              </colgroup>
              <thead>
                <tr>
                  <th>凭证</th>
                  <th>往来对象</th>
                  <th>事项</th>
                  <th>状态</th>
                  <th class="number">{{ isClosed ? "关账时点金额" : "期末金额" }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="item in category.items"
                  :key="`${item.voucher}-${item.party}-${item.description}`"
                >
                  <td data-label="凭证">{{ item.voucher }}</td>
                  <td data-label="往来对象">{{ item.party }}</td>
                  <td data-label="事项">{{ item.description }}</td>
                  <td data-label="状态">
                    <span class="status">{{ openStateLabel(category.direction, item.status) }}</span>
                  </td>
                  <td class="number" :data-label="isClosed ? '关账时点金额' : '期末金额'">
                    {{ formatFen(item.outstanding_fen) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </details>
      <p v-if="!openItems.total_count" class="empty">
        {{ isClosed ? "该月关账时没有应收或应付余额。" : "期末没有未完全结清的应收或应付事项。" }}
      </p>
    </div>
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
.category summary {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.section-heading {
  align-items: flex-end;
  margin-bottom: 15px;
}

.section-kicker {
  margin: 0 0 4px;
  color: var(--brief-green);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

h2,
p {
  margin-top: 0;
}

h2 {
  margin-bottom: 0;
  font-size: 23px;
  letter-spacing: -0.025em;
}

.section-heading > strong {
  color: var(--brief-muted);
  font-size: 13px;
}

.historical-note {
  margin: -3px 0 12px;
  padding: 9px 12px;
  border-left: 3px solid var(--brief-blue);
  border-radius: 7px;
  background: color-mix(in srgb, var(--brief-blue-soft) 62%, var(--brief-surface));
  color: var(--brief-muted);
  font-size: 12px;
  line-height: 1.55;
}

.open-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 11px;
}

.open-summary article {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 2px 14px;
  align-items: center;
  padding: 13px 15px;
  border-radius: 13px;
}

.open-summary .receivable {
  background: var(--brief-blue-soft);
  color: var(--brief-blue);
}

.open-summary .payable {
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.open-summary article > span,
.open-summary article > small {
  grid-column: 1;
}

.open-summary article > strong {
  grid-row: 1 / 3;
  grid-column: 2;
  font-size: 21px;
}

.categories {
  display: grid;
  gap: 8px;
}

.category {
  border: 1px solid var(--brief-line);
  border-radius: 13px;
  background: var(--brief-soft);
}

.category[open] {
  background: var(--brief-surface);
}

.category summary {
  position: relative;
  min-height: 56px;
  align-items: center;
  padding: 8px 40px 8px 12px;
  list-style: none;
  cursor: pointer;
}

.category summary::-webkit-details-marker {
  display: none;
}

.category summary:hover {
  background: color-mix(in srgb, var(--brief-green-soft) 50%, transparent);
}

.category-name,
.category-total {
  display: grid;
  gap: 3px;
}

.category-name small,
.category-total small {
  color: var(--brief-muted);
  font-size: 11px;
}

.category-total {
  justify-items: end;
  white-space: nowrap;
}

.chevron {
  position: absolute;
  top: 50%;
  right: 15px;
  color: var(--brief-muted);
  font-size: 18px;
  transform: translateY(-50%);
  transition: transform 160ms ease;
}

.category[open] .chevron {
  transform: translateY(-50%) rotate(180deg);
}

.category-detail {
  padding: 0 14px 14px;
  border-top: 1px solid var(--brief-line);
}

.party-groups {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 7px;
  margin: 12px 0;
}

.party-groups article {
  display: grid;
  gap: 2px;
  padding: 10px;
  border-radius: 10px;
  background: var(--brief-soft);
}

.party-groups article span,
.party-groups article small {
  overflow: hidden;
  color: var(--brief-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  min-width: 880px;
  border-collapse: collapse;
  font-size: 12px;
  table-layout: fixed;
}

.voucher-column {
  width: 112px;
}

.party-column {
  width: 260px;
}

.status-column {
  width: 100px;
}

.amount-column {
  width: 128px;
}

th,
td {
  padding: 9px 8px;
  border-bottom: 1px solid var(--brief-line);
  text-align: left;
}

th {
  color: var(--brief-muted);
  font-size: 11px;
}

.number {
  text-align: right;
  white-space: nowrap;
}

td[data-label="凭证"] {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

td[data-label="往来对象"],
td[data-label="事项"] {
  overflow-wrap: anywhere;
}

.status {
  display: inline-flex;
  min-height: 23px;
  align-items: center;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
  font-size: 11px;
  font-weight: 750;
  white-space: nowrap;
}

.empty {
  margin: 0;
  padding: 24px;
  border: 1px dashed var(--brief-line);
  border-radius: 12px;
  color: var(--brief-muted);
  text-align: center;
}

@media (max-width: 680px) {
  .brief-section {
    padding: 17px;
    border-radius: 17px;
  }

  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 7px;
  }

  .open-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .category summary {
    gap: 9px;
  }

  .category-name {
    min-width: 0;
  }

  .category-name small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .open-summary article {
    display: grid;
    grid-template-columns: 1fr;
    padding: 10px 11px;
  }

  .open-summary article > span,
  .open-summary article > small,
  .open-summary article > strong {
    grid-row: auto;
    grid-column: 1;
  }

  .open-summary article > strong {
    font-size: 17px;
  }

  .party-groups {
    grid-template-columns: 1fr;
  }

  table,
  tbody,
  tr,
  td {
    display: block;
    width: 100%;
  }

  table {
    min-width: 0;
    table-layout: auto;
  }

  colgroup {
    display: none;
  }

  thead {
    display: none;
  }

  tr {
    padding: 8px 0;
    border-bottom: 1px solid var(--brief-line);
  }

  td {
    display: grid;
    grid-template-columns: 80px minmax(0, 1fr);
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
}
</style>
