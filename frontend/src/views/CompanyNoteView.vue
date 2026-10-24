<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchCompanyNote, type CompanyNoteResponse } from "../api/companyNote";
import CompanyNoteBody from "../components/CompanyNoteBody.vue";
import { useDashboardContext } from "../composables/useDashboardContext";

const route = useRoute();
const { context } = useDashboardContext();
const companyId = computed(() => typeof route.query.company_id === "string" ? route.query.company_id : "");
const response = shallowRef<CompanyNoteResponse | null>(null);
const companyName = computed(() => response.value?.company.name
  ?? (context.value?.current_company?.company_id === companyId.value ? context.value.current_company.name : ""));
const loading = ref(false), failed = ref(false);
let generation = 0, request: AbortController | null = null;

function cancel() { generation += 1; request?.abort(); request = null; }

async function load() {
  cancel();
  const selected = companyId.value, selectedGeneration = generation;
  failed.value = false;
  if (!selected) { loading.value = false; response.value = null; return; }
  const active = new AbortController(); request = active; loading.value = true;
  const current = () => generation === selectedGeneration && companyId.value === selected && !active.signal.aborted;
  try {
    const loaded = await fetchCompanyNote(selected, active.signal);
    if (current()) response.value = loaded;
  } catch {
    if (current()) failed.value = true;
  } finally {
    if (current()) { loading.value = false; request = null; }
  }
}

watch(companyId, () => { response.value = null; failed.value = false; void load(); }, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { cancel(); response.value = null; failed.value = false; loading.value = false; });
</script>

<template>
  <section class="company-note-page" aria-labelledby="company-note-heading" :aria-busy="loading">
    <header class="company-note-heading">
      <div><p v-if="companyName" class="company-name">{{ companyName }}</p><div class="company-note-title"><h1 id="company-note-heading">公司说明</h1><span class="readonly-badge">只读</span></div></div>
      <button class="dashboard-action" type="button" :disabled="!companyId || loading" @click="load">{{ failed ? '重试' : '刷新' }}</button>
    </header>
    <p v-if="!companyId" class="note-notice">请先选择要查看的公司。</p>
    <p v-if="loading" class="note-notice" role="status">{{ response ? '正在刷新公司说明…' : '正在读取公司说明…' }}</p>
    <p v-if="failed" class="note-error" role="alert">{{ response ? '未能取得新内容，下面仍是上次读取的公司说明。请重试。' : '公司说明暂时无法读取，请重试。' }}</p>
    <div v-if="response" class="company-note-content">
      <CompanyNoteBody v-if="response.description && response.description.text.trim()" :text="response.description.text" :format="response.description.text_format" />
      <p v-else class="note-notice">这家公司还没有公司说明。</p>
    </div>
  </section>
</template>

<style scoped>
.company-note-page { width: 100%; max-width: 1000px; min-width: 0; margin: 0 auto; padding: clamp(20px, 4vw, 48px); }
.company-note-heading { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 28px; }
.company-note-heading > div { min-width: 0; }
.company-note-heading > button { flex-shrink: 0; white-space: nowrap; }
.company-name { margin: 0 0 7px; color: var(--muted); font-size: 14px; overflow-wrap: anywhere; }
.company-note-title { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.company-note-title h1 { margin: 0; font-size: 26px; }
.readonly-badge { padding: 3px 8px; border: 1px solid var(--line); border-radius: 6px; color: var(--muted); font-size: 12px; }
.company-note-content { min-width: 0; padding: clamp(18px, 3vw, 32px); border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface); }
.note-notice { color: var(--muted); line-height: 1.7; }
.note-error { padding: 14px 18px; border: 1px solid var(--line); border-radius: var(--radius-control); color: var(--danger); line-height: 1.7; }
@media (max-width: 600px) { .company-note-page { padding: 20px 12px; } .company-note-title h1 { font-size: 23px; } .company-note-heading { gap: 12px; } }
</style>
