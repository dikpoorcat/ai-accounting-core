<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { fetchLocalJobs, localErrorMessage, localJobName, localJobStatus, localJobMessage, localJobDownloadAvailable, type LocalJob } from "../api/localKernel";
import { fetchQuarterlyWorkbook } from "../api/reports";
import { RouterLink } from "vue-router";
const props = defineProps<{ companyId: string }>();
const jobs = ref<LocalJob[]>([]);
const loading = ref(false);
const error = ref("");
const downloadingId = ref("");
const downloadError = ref("");
let controller: AbortController | undefined;
let downloadController: AbortController | undefined;
function fileTitle(job: LocalJob) {
  if (job.download_file_name) return job.download_file_name;
  const source = job.report_source;
  return job.kind === "report_export" && source
    ? `${source.year} 年第 ${source.quarter} 季度财务报表`
    : localJobName(job.kind);
}
function fileStatus(job: LocalJob) {
  if (job.delivery_status === "invalid") return "文件无法下载";
  if (localJobDownloadAvailable(job)) return "可以下载";
  if (job.status === "pending") return "等待生成";
  return localJobStatus(job.status);
}
function fileMessage(job: LocalJob) {
  if (job.delivery_status === "invalid") return "原文件已失效或无法确认完整性，请重新生成。";
  if (job.status === "failed" && job.attempts >= 3) return job.kind === "report_export"
    ? "本次生成未成功，请到财务报表页重新生成。"
    : "本次生成未成功，请处理失败原因后重新生成。";
  if (job.status === "failed") return "本次生成未成功，稍后刷新可查看重试结果。";
  if (localJobDownloadAvailable(job)) return "文件已准备好，可以下载。";
  return localJobMessage(job);
}
function reportTarget(job: LocalJob) {
  const source = job.report_source;
  return { path: "/reports", query: {
    company_id: props.companyId,
    quarter: source ? `${source.year}-Q${source.quarter}` : undefined,
    period: source ? `${source.year}-${String(source.quarter * 3).padStart(2, "0")}` : undefined,
    carry_forward_fact_id: source?.carry_forward_fact_id || undefined,
  } };
}
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
    <header><div><h2 id="jobs-heading">文件与处理进度</h2><p>当前公司最近 20 项文件处理记录，包含各个月份。</p></div><button class="dashboard-action" :disabled="loading" @click="load">刷新进度</button></header>
    <p v-if="downloadError" role="alert">{{ downloadError }}</p>
    <p v-if="loading" role="status">正在读取文件进度…</p><p v-else-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!jobs.length">本公司还没有生成备份、代发或报表文件的记录。</p>
    <ul v-else>
      <li v-for="job in jobs" :key="job.id">
        <div><strong>{{ fileTitle(job) }}</strong><span>{{ fileStatus(job) }}</span></div>
        <p>{{ fileMessage(job) }}</p>
        <button v-if="localJobDownloadAvailable(job)" class="dashboard-action" :disabled="Boolean(downloadingId)" @click="download(job)">{{ downloadingId === job.id ? "正在下载…" : "下载报表" }}</button>
        <RouterLink v-if="job.kind === 'report_export' && (job.delivery_status === 'invalid' || (job.status === 'failed' && job.attempts >= 3))" :to="reportTarget(job)">到财务报表重新生成</RouterLink>
        <details><summary>{{ job.last_error ? "供核对的失败详情" : "供核对的处理详情" }}</summary><pre>{{ JSON.stringify(job, null, 2) }}</pre></details>
      </li>
    </ul>
  </section>
</template>
<style scoped>
.jobs-panel { padding: 22px; margin-bottom: 22px; } header, li>div { display: flex; justify-content: space-between; align-items: center; gap: 12px; } h2 { margin: 0; font-size: 19px; } p, summary { color: var(--muted); font-size: 13px; } ul { list-style: none; padding: 0; max-height: 420px; overflow: auto; } li { padding: 14px 0; border-top: 1px solid var(--line); } summary { cursor: pointer; } pre { overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
li>div strong { min-width: 0; overflow-wrap: anywhere; }
li>div span { flex-shrink: 0; }
</style>
