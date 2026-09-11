<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { fetchLocalJobs, localErrorMessage, localJobName, localJobStatus, localJobMessage, localJobDownloadAvailable, type LocalJob } from "../api/localKernel";
import { fetchQuarterlyWorkbook } from "../api/reports";
const props = defineProps<{ companyId: string }>();
const jobs = ref<LocalJob[]>([]);
const loading = ref(false);
const error = ref("");
const downloadingId = ref("");
const downloadError = ref("");
let controller: AbortController | undefined;
let downloadController: AbortController | undefined;
async function load() {
  controller?.abort();
  downloadController?.abort();
  downloadingId.value = ""; downloadError.value = "";
  const active = new AbortController(); controller = active;
  jobs.value = []; error.value = ""; loading.value = true;
  try { const rows = await fetchLocalJobs(props.companyId, active.signal); if (!active.signal.aborted) jobs.value = rows; }
  catch (caught) { if (!active.signal.aborted) error.value = localErrorMessage(caught); }
  finally { if (!active.signal.aborted) loading.value = false; }
}
async function download(job: LocalJob) {
  if (!localJobDownloadAvailable(job) || downloadingId.value) return;
  downloadController?.abort();
  const active = new AbortController(); downloadController = active;
  const companyId = props.companyId;
  downloadingId.value = job.id; downloadError.value = "";
  try {
    const blob = await fetchQuarterlyWorkbook(companyId, job.id, active.signal);
    if (active.signal.aborted || props.companyId !== companyId) return;
    const href = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = href; link.download = job.download_file_name || "季度财务报表.xlsx";
    document.body.append(link); link.click(); link.remove(); URL.revokeObjectURL(href);
  } catch (caught) {
    if (!active.signal.aborted && props.companyId === companyId) downloadError.value = localErrorMessage(caught);
  } finally {
    if (downloadController === active) downloadingId.value = "";
  }
}
watch(() => props.companyId, () => { void load(); }, { immediate: true });
onBeforeUnmount(() => { controller?.abort(); downloadController?.abort(); });
</script>
<template>
  <section class="jobs-panel panel" aria-labelledby="jobs-heading" :aria-busy="loading">
    <header><div><h2 id="jobs-heading">最近后台任务</h2><p>当前公司最近 20 项，不限核算月份。</p></div><button class="dashboard-action" :disabled="loading" @click="load">刷新任务</button></header>
    <p v-if="downloadError" role="alert">{{ downloadError }}</p>
    <p v-if="loading" role="status">正在读取后台任务…</p><p v-else-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!jobs.length">本公司暂无备份、代发或报表生成任务。</p>
    <ul v-else><li v-for="job in jobs" :key="job.id"><div><strong>{{ localJobName(job.kind) }}</strong><span>{{ localJobStatus(job.status) }}</span></div><p>{{ localJobMessage(job) }}</p><button v-if="localJobDownloadAvailable(job)" class="dashboard-action" :disabled="Boolean(downloadingId)" @click="download(job)">{{ downloadingId === job.id ? "正在下载…" : "下载报表" }}</button><details><summary>{{ job.last_error ? "查看失败技术信息" : "查看任务技术信息" }}</summary><pre>{{ JSON.stringify(job, null, 2) }}</pre></details></li></ul>
  </section>
</template>
<style scoped>
.jobs-panel { padding: 22px; margin-bottom: 22px; } header, li>div { display: flex; justify-content: space-between; align-items: center; gap: 12px; } h2 { margin: 0; font-size: 19px; } p, summary { color: var(--muted); font-size: 13px; } ul { list-style: none; padding: 0; max-height: 420px; overflow: auto; } li { padding: 14px 0; border-top: 1px solid var(--line); } summary { cursor: pointer; } pre { overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
</style>
