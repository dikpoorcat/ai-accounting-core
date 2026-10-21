<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";
import { DashboardApiError, dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import { fetchMaterials, fetchSourceFile, fetchSourceText, parseMaterialsQuery, type MaterialSource, type ReviewMaterialsQuery, type ReviewMaterialsResponse } from "../api/reviewMaterials";

const route = useRoute();
const response = shallowRef<ReviewMaterialsResponse | null>(null);
const rows = ref<MaterialSource[]>([]);
const page = shallowRef<ReviewMaterialsResponse["page"] | null>(null);
const query = shallowRef<ReviewMaterialsQuery | null>(null);
const loading = ref(false), loadingMore = ref(false), error = ref(""), stale = ref(false);
const selectedSource = ref(""), text = ref(""), contentLoading = ref(false), contentError = ref("");
let generation = 0, contentGeneration = 0;
let listRequest: AbortController | null = null, contentRequest: AbortController | null = null;
let downloadUrl: string | null = null;
const selectionLabel = computed(() => response.value?.selection.mode === "frozen" ? "已关账时采用的资料" : response.value?.selection.mode === "open_result" ? "指定入账结果的资料" : "当前有效资料");

function closeContent() {
  contentGeneration += 1;
  contentRequest?.abort(); contentRequest = null;
  selectedSource.value = ""; text.value = ""; contentError.value = ""; contentLoading.value = false;
  if (downloadUrl) { URL.revokeObjectURL(downloadUrl); downloadUrl = null; }
}
function cancel() { generation += 1; listRequest?.abort(); listRequest = null; closeContent(); }
function reportError(caught: unknown) {
  if (isDashboardSnapshotChanged(caught)) {
    stale.value = true; closeContent();
    return "所选资料已变化，请重新读取；指定的旧入账结果不会自动换成当前结果。";
  }
  return dashboardErrorMessage(caught);
}

async function first(refresh = false) {
  cancel();
  const selectedGeneration = generation;
  response.value = null; rows.value = []; page.value = null; query.value = null;
  loading.value = true; loadingMore.value = false; error.value = ""; stale.value = false;
  const request = new AbortController(); listRequest = request;
  try {
    const selected = parseMaterialsQuery(new URL(route.fullPath, window.location.origin).searchParams);
    if (refresh) { delete selected.cursor; delete selected.expected_version; }
    query.value = selected;
    const loaded = await fetchMaterials({ limit: 20, ...selected }, request.signal);
    if (selectedGeneration !== generation) return;
    if (!selected.cursor && !loaded.page.has_more && loaded.sources.length !== loaded.page.filtered_count) throw new DashboardApiError(502, "REVIEW_RESPONSE_MISMATCH", "来源清单未完整返回，请重新读取。");
    response.value = loaded; rows.value = [...loaded.sources]; page.value = loaded.page;
  } catch (caught) {
    if (selectedGeneration === generation && !request.signal.aborted) error.value = reportError(caught);
  } finally {
    if (selectedGeneration === generation) { loading.value = false; listRequest = null; }
  }
}

async function more() {
  if (!response.value || !query.value || !page.value?.next_cursor || loadingMore.value || stale.value) return;
  const selectedGeneration = generation, previous = response.value;
  const request = new AbortController(); listRequest = request;
  loadingMore.value = true; error.value = "";
  try {
    const loaded = await fetchMaterials({ ...query.value, limit: 500, cursor: page.value.next_cursor, expected_version: previous.snapshot_version }, request.signal, previous);
    if (selectedGeneration !== generation) return;
    if (rows.value.length && loaded.sources.length && loaded.sources[0]!.source_id <= rows.value.at(-1)!.source_id) throw new DashboardApiError(502, "REVIEW_RESPONSE_MISMATCH", "资料分页重复或顺序不一致，请重新读取。");
    const count = rows.value.length + loaded.sources.length;
    if (count > loaded.page.filtered_count || (!query.value.cursor && !loaded.page.has_more && count !== loaded.page.filtered_count)) throw new DashboardApiError(502, "REVIEW_RESPONSE_MISMATCH", "来源分页数量不一致，请重新读取。");
    rows.value.push(...loaded.sources); page.value = loaded.page;
  } catch (caught) {
    if (selectedGeneration === generation && !request.signal.aborted) error.value = reportError(caught);
  } finally {
    if (selectedGeneration === generation) { loadingMore.value = false; listRequest = null; }
  }
}

async function open(row: MaterialSource) {
  if (!row.available || stale.value || !response.value || !query.value) return;
  if (selectedSource.value === row.source_id && row.kind === "owner_statement") { closeContent(); return; }
  closeContent();
  const selectedGeneration = generation, selectedContent = contentGeneration;
  const request = new AbortController(); contentRequest = request;
  selectedSource.value = row.source_id; contentLoading.value = true;
  try {
    if (row.kind === "owner_statement") {
      const loaded = await fetchSourceText(query.value, response.value, row, request.signal);
      if (selectedGeneration === generation && selectedContent === contentGeneration) text.value = loaded;
    } else {
      const loaded = await fetchSourceFile(query.value, response.value, row, request.signal);
      if (selectedGeneration !== generation || selectedContent !== contentGeneration) return;
      downloadUrl = URL.createObjectURL(new Blob([loaded.blob], { type: "application/octet-stream" }));
      const anchor = document.createElement("a"); anchor.href = downloadUrl; anchor.download = loaded.receipt.file_name;
      anchor.click();
    }
  } catch (caught) {
    if (selectedGeneration === generation && selectedContent === contentGeneration && !request.signal.aborted) {
      const message = reportError(caught);
      if (stale.value) error.value = message;
      else contentError.value = message;
    }
  } finally {
    if (selectedGeneration === generation && selectedContent === contentGeneration) { contentLoading.value = false; contentRequest = null; }
  }
}

watch(() => route.fullPath, () => { void first(); }, { immediate: true });
onBeforeUnmount(cancel);
</script>

<template>
  <section class="materials-page" aria-labelledby="materials-heading" :aria-busy="loading">
    <header class="materials-heading">
      <div><h1 id="materials-heading">业务来源查阅</h1><p>只读查看这笔业务所需的原件和老板补充说明。</p></div>
      <button class="dashboard-action" :disabled="loading || loadingMore" @click="first(true)">重新读取</button>
    </header>
    <p v-if="loading" role="status">正在读取来源…</p>
    <p v-if="error" class="materials-error" role="alert">{{ error }}</p>
    <template v-if="response">
      <section class="materials-summary"><h2>{{ response.business_label }}</h2><p>{{ selectionLabel }}<template v-if="response.selection.mode !== 'current'"> · {{ response.selection.posting_period }}</template></p><p>共 {{ page?.total_count }} 份来源，已显示 {{ rows.length }} 份。</p></section>
      <p v-if="!rows.length" class="materials-empty">这笔业务没有需要查阅的来源。</p>
      <ul v-else class="materials-list">
        <li v-for="row in rows" :key="row.source_id" class="material-row">
          <div class="material-heading"><div><h3>{{ row.label }}</h3><p>{{ row.kind === 'file' ? '原始文件' : '老板补充说明' }}<template v-if="row.archive_month"> · {{ row.archive_month }}</template></p></div>
            <button class="dashboard-action" :disabled="!row.available || stale || (contentLoading && selectedSource === row.source_id)" @click="open(row)">{{ contentLoading && selectedSource === row.source_id ? '正在读取…' : row.kind === 'file' ? '下载原件' : selectedSource === row.source_id ? '收起说明' : '查看说明' }}</button>
          </div>
          <ul v-if="row.locations.length" class="material-locations" aria-label="资料位置"><li v-for="location in row.locations" :key="location">{{ location }}</li></ul>
          <p v-if="!row.available" class="materials-error">{{ row.unavailable_reason === 'missing_file' ? '原件暂时缺失，请联系会计 AI 补回。' : '原件内容与登记资料不符，请联系会计 AI 核查。' }}</p>
          <template v-if="selectedSource === row.source_id"><p v-if="contentError" class="materials-error" role="alert">{{ contentError }}</p><pre v-if="text" class="material-text">{{ text }}</pre></template>
        </li>
      </ul>
      <button v-if="page?.has_more" class="dashboard-action materials-more" :disabled="loadingMore || stale" @click="more">{{ loadingMore ? '正在读取…' : '加载更多来源' }}</button>
    </template>
  </section>
</template>

<style scoped>
.materials-page { width: min(960px, 100%); margin: 0 auto; padding: 28px 24px 48px; }
.materials-heading, .material-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
h1 { margin: 0; font-size: 25px; } h2 { margin: 0; font-size: 19px; } h3 { margin: 0; font-size: 16px; overflow-wrap: anywhere; }
p { margin: 8px 0; line-height: 1.7; color: var(--muted); } button { flex: none; }
.materials-summary { margin-top: 26px; padding: 20px; border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface); }
.materials-list { display: grid; gap: 16px; padding: 0; margin: 20px 0; list-style: none; }
.material-row { padding: 20px; border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface); min-width: 0; }
.material-locations { margin: 12px 0 0; padding-left: 20px; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.material-text { margin: 16px 0 0; padding: 16px; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.8; background: var(--surface-soft); border-radius: var(--radius-control); }
.materials-error { color: var(--danger); overflow-wrap: anywhere; } .materials-empty { padding: 24px 0; } .materials-more { display: block; margin: 20px auto; }
@media (max-width: 600px) { .materials-page { padding: 20px 16px 36px; } .materials-heading, .material-heading { flex-wrap: wrap; gap: 12px; } h1 { font-size: 22px; } .material-row, .materials-summary { padding: 16px; } }
</style>
