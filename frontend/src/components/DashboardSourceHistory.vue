<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchAssetsDashboard } from "../api/assets";
import { fetchEmployeesDashboard } from "../api/employees";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import type { DashboardCollection } from "../api/dashboardContracts";
import DashboardPagination from "./DashboardPagination.vue";
import DashboardBusinessRecords from "./DashboardBusinessRecords.vue";
const props = defineProps<{ endpoint: "employees" | "assets"; section: "source_history" | "settlement_events"; entityId: string; period: string; snapshotVersion: string; title: string }>();
const emit = defineEmits<{ changed: [] }>();
const route = useRoute();
const collection = ref<DashboardCollection | null>(null), loading = ref(false), error = ref("");
const retryMore = ref(false), notice = ref("");
let generation = 0, mounted = true, controller: AbortController | null = null;
function selection() { return JSON.stringify([route.query.company_id, props.endpoint, props.section, props.entityId, props.period, props.snapshotVersion]); }
function invalidate() { generation += 1; controller?.abort(); controller = null; collection.value = null; loading.value = false; error.value = ""; retryMore.value = false; notice.value = ""; }
async function load(more = false) {
  const current = collection.value;
  if (loading.value || (more && !current?.page.next_cursor)) return;
  const version = ++generation, key = selection(), request = new AbortController();
  controller = request; loading.value = true; error.value = ""; retryMore.value = more;
  const valid = () => mounted && generation === version && selection() === key && controller === request;
  try {
    const options = { section: props.section, cursor: more ? current?.page.next_cursor ?? undefined : undefined, expected_version: props.snapshotVersion };
    const response = props.endpoint === "assets"
      ? await fetchAssetsDashboard(props.period, request.signal, { ...options, asset_id: props.entityId })
      : await fetchEmployeesDashboard(props.period, request.signal, { ...options, section: "settlement_events", employee_id: props.entityId });
    if (!valid() || !response.data) return;
    const next = response.data.collections[props.section];
    collection.value = { ...next, items: [...(more ? current?.items ?? [] : []), ...next.items] };
  } catch (caught) { if (valid()) { if (isDashboardSnapshotChanged(caught)) { invalidate(); notice.value = "来源资料已更新，正在重新读取。"; emit("changed"); } else error.value = dashboardErrorMessage(caught); } }
  finally { if (valid()) loading.value = false; }
}
function opened(event: Event) { if ((event.target as HTMLDetailsElement).open && !collection.value) void load(); }
watch(selection, invalidate, { flush: "sync" });
onBeforeUnmount(() => { mounted = false; invalidate(); });
</script>

<template>
  <details @toggle="opened">
    <summary>{{ title }}</summary>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading">正在读取…</p>
    <p v-if="error && !collection" role="alert">{{ error }} <button type="button" @click="load(retryMore)">重新读取</button></p>
    <template v-if="collection">
      <DashboardBusinessRecords :items="collection.items" :period="period" :snapshot-version="snapshotVersion" @changed="emit('changed')" />
      <DashboardPagination :page="collection.page" :loaded="collection.items.length" :loading="loading" :error="error" @more="load(true)" @retry="load(retryMore)" />
    </template>
  </details>
</template>
