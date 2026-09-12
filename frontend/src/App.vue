<!-- @format -->

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { DeepReadonly } from "vue";
import { RouterLink, RouterView, useRoute, useRouter } from "vue-router";

import OwnerSessionPanel from "./components/OwnerSessionPanel.vue";
import BackgroundJobsPanel from "./components/BackgroundJobsPanel.vue";
import DashboardMonthPicker from "./components/DashboardMonthPicker.vue";
import { useDashboardContext } from "./composables/useDashboardContext";
import type { DashboardContext } from "./api/context";

type Theme = "light" | "dark";
defineProps<{ launchError?: string }>();

const appVersion = __APP_VERSION__;
const navItems = [
  { name: "brief", label: "经营简报", path: "/", icon: "chart" },
  { name: "funds", label: "资金", path: "/funds", icon: "wallet" },
  { name: "employees", label: "员工", path: "/employees", icon: "people" },
  { name: "assets", label: "资产", path: "/assets", icon: "box" },
  { name: "reports", label: "财务报表", path: "/reports", icon: "report" },
] as const;

const authenticated = ref(false);
const showSecurity = ref(false);
const showJobs = ref(false);
const contextError = ref("");
let contextGeneration = 0, mounted = true;
let activeContextLoad: number | null = null;
localStorage.removeItem("finance-dashboard-org-id");
const route = useRoute();
const router = useRouter();
const savedTheme = localStorage.getItem("finance-dashboard-theme");
const theme = ref<Theme>(savedTheme === "dark" ? "dark" : "light");
const {
  context,
  load: loadContext,
  cancel: cancelContext,
  loading: contextLoading,
  selectionNotice,
  setSelectionNotice,
} = useDashboardContext();
let selectionNoticeTimer: ReturnType<typeof setTimeout> | undefined;
watch(selectionNotice, (message) => {
  clearTimeout(selectionNoticeTimer);
  if (message) selectionNoticeTimer = setTimeout(() => setSelectionNotice(""), 6000);
}, { flush: "sync" });
onBeforeUnmount(() => clearTimeout(selectionNoticeTimer));
const companyName = computed(() => context.value?.company || "公司财务看板");
const companies = computed(() => context.value?.companies ?? []);
const currentCompany = computed(() => context.value?.current_company ?? null);
const sidebarCollapsed = ref(
  localStorage.getItem("finance-dashboard-sidebar") === "collapsed",
);
const brandInitial = computed(
  () => companyName.value.trim().slice(0, 1) || "财",
);
const periods = computed(() => context.value?.periods ?? []);
const selectedPeriod = computed(() => {
  const requested =
    typeof route.query.period === "string" ? route.query.period : null;
  if (periods.value.some((period) => period.key === requested)) {
    return requested ?? "";
  }
  const defaultPeriod = context.value?.default_period;
  if (periods.value.some((period) => period.key === defaultPeriod)) {
    return defaultPeriod ?? "";
  }
  return periods.value.at(-1)?.key ?? "";
});

async function applyContextSelection(loaded: DeepReadonly<DashboardContext>, valid: () => boolean) {
  if (!valid()) return;
  const companyId = loaded.current_company?.company_id;
  if (!companyId) return;
  const requestedPeriod = typeof route.query.period === "string" ? route.query.period : undefined;
  const quarter = route.name === "reports" && typeof route.query.quarter === "string" && loaded.quarters.some(item => item.key === route.query.quarter)
    ? route.query.quarter : undefined;
  const quarterPeriod = quarter ? loaded.periods
    .filter(item => `${item.year}-Q${Math.ceil(item.month / 3)}` === quarter)
    .reduce<string | undefined>((latest, item) => !latest || item.key > latest ? item.key : latest, undefined) : undefined;
  const period = requestedPeriod && loaded.periods.some(item => item.key === requestedPeriod)
    ? requestedPeriod : quarterPeriod ?? loaded.default_period ?? undefined;
  localStorage.setItem("finance-dashboard-company-id", companyId);
  if (requestedPeriod && requestedPeriod !== period) {
    setSelectionNotice(period ? `所选月份已不可查看，已切换至 ${period}。` : "该公司还没有可查看的月份。");
  }
  if (route.query.company_id !== companyId || route.query.period !== period || route.query.quarter !== quarter || route.query.org_id) {
    await router.replace({ query: { ...route.query, org_id: undefined, company_id: companyId, period, quarter } });
  }
}

async function loadCompanyContext() {
  const generation = ++contextGeneration;
  const selection = JSON.stringify([route.query.company_id, route.query.period, route.query.quarter]);
  const valid = () => mounted && contextGeneration === generation && authenticated.value && JSON.stringify([route.query.company_id, route.query.period, route.query.quarter]) === selection;
  cancelContext();
  contextError.value = "";
  if (!authenticated.value) { activeContextLoad = null; return; }
  activeContextLoad = generation;
  const requested = typeof route.query.company_id === "string" ? route.query.company_id : undefined;
  try {
    const loaded = await loadContext(true);
    if (!valid()) return;
    const companyId = loaded.current_company?.company_id;
    if (!companyId) return;
    const saved = localStorage.getItem("finance-dashboard-company-id");
    if (!requested && saved && saved !== companyId && loaded.companies.some(item => item.company_id === saved)) {
      await router.replace({ query: { ...route.query, company_id: saved } });
      return;
    }
    await applyContextSelection(loaded, valid);
  } catch (caught) {
    if (!valid()) return;
    if (caught instanceof DOMException && caught.name === "AbortError") return;
    contextError.value = caught instanceof Error ? caught.message : "公司资料读取失败，请重试。";
  } finally {
    if (activeContextLoad === generation) activeContextLoad = null;
  }
}
function setAuthenticated(value: boolean) {
  authenticated.value = value;
  if (value) showSecurity.value = false;
  if (!value) { cancelContext(); showSecurity.value = true; }
}
function sessionExpired() { setAuthenticated(false); }
watch(
  [authenticated, () => route.query.company_id, () => route.query.period, () => route.query.quarter],
  ([isAuthenticated, companyId, period, quarter], [wasAuthenticated, previousCompany, previousPeriod, previousQuarter]) => {
    if (isAuthenticated !== wasAuthenticated || companyId !== previousCompany) {
      void loadCompanyContext();
      return;
    }
    if (period !== previousPeriod || quarter !== previousQuarter) {
      contextGeneration += 1;
      if (isAuthenticated && !context.value) void loadCompanyContext();
    }
  },
  { flush: "sync" },
);
watch(context, async loaded => {
  // Initial company loads resolve selection in loadCompanyContext, including saved-company recovery.
  // An in-place page refresh reuses the loaded context and must not start another context fetch.
  if (!loaded || activeContextLoad !== null || loaded.current_company?.company_id !== route.query.company_id) return;
  const generation = contextGeneration;
  const selection = JSON.stringify([route.query.company_id, route.query.period, route.query.quarter]);
  const valid = () => mounted && authenticated.value && contextGeneration === generation && context.value === loaded
    && JSON.stringify([route.query.company_id, route.query.period, route.query.quarter]) === selection;
  try { await applyContextSelection(loaded, valid); }
  catch (caught) { if (valid()) contextError.value = caught instanceof Error ? caught.message : "公司期间读取失败，请重试。"; }
});
onMounted(async () => {
  window.addEventListener("finance-session-expired", sessionExpired);
  if (route.query.org_id) await router.replace({ query: { ...route.query, org_id: undefined } });
});
onBeforeUnmount(() => { mounted = false; contextGeneration += 1; cancelContext(); window.removeEventListener("finance-session-expired", sessionExpired); });

watch(
  theme,
  (value) => {
    document.documentElement.dataset.theme = value;
    localStorage.setItem("finance-dashboard-theme", value);
  },
  { immediate: true },
);

function toggleTheme() {
  theme.value = theme.value === "dark" ? "light" : "dark";
}

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value;
  localStorage.setItem(
    "finance-dashboard-sidebar",
    sidebarCollapsed.value ? "collapsed" : "expanded",
  );
}

async function selectCompany(companyId: string) {
  if (route.query.company_id === companyId) return;
  setSelectionNotice("");
  cancelContext();
  await router.push({
    query: {
      company_id: companyId,
      period: route.query.period,
    },
    hash: "",
  });
}

async function selectPeriod(periodKey: string) {
  const period = periods.value.find((item) => item.key === periodKey);
  if (!period) return;
  setSelectionNotice("");
  const quarter =
    route.name === "reports"
      ? `${period.year}-Q${Math.ceil(period.month / 3)}`
      : undefined;
  if (
    route.query.period === periodKey &&
    (route.name !== "reports" || route.query.quarter === quarter)
  ) {
    return;
  }
  await router.push({
    query: {
      company_id: currentCompany.value?.company_id ?? route.query.company_id,
      period: periodKey,
      quarter,
    },
    hash: "",
  });
}

</script>

<template>
  <a class="skip-link" href="#workspace-content">跳到主要内容</a>
  <div class="app-shell" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
    <header id="workspace-sidebar" class="workspace-sidebar">
      <button
        class="sidebar-toggle"
        type="button"
        :aria-label="sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'"
        :aria-expanded="!sidebarCollapsed"
        aria-controls="workspace-sidebar"
        :title="sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'"
        @click="toggleSidebar"
      >
        <svg
          class="sidebar-icon sidebar-toggle-icon"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path d="m14 6-6 6 6 6" />
        </svg>
      </button>

      <div class="sidebar-scroll-area">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true">{{ brandInitial }}</span>
          <div v-if="companies.length" class="company-switcher">
            <strong class="company-switcher-name" aria-hidden="true">{{ companyName }}</strong>
            <select
              :value="currentCompany?.company_id"
              aria-label="切换公司"
              @change="selectCompany(($event.target as HTMLSelectElement).value)"
            >
              <option
                v-for="company in companies"
                :key="company.company_id"
                :value="company.company_id"
              >
                {{ company.name }}{{ company.status === "archived" ? "（已归档）" : "" }}
              </option>
            </select>
            <small
              v-if="currentCompany?.status === 'archived'"
              class="archived-company-badge"
            >
              只读 · 已归档
            </small>
          </div>
          <div v-else class="brand-copy">
            <strong class="brand-name">{{ companyName }}</strong>
          </div>
        </div>

        <nav v-if="authenticated" class="module-nav" aria-label="看板模块">
          <RouterLink
            v-for="item in navItems"
            :key="item.name"
            :to="{ path: item.path, query: { company_id: route.query.company_id, period: route.query.period } }"
            class="module-link"
            :aria-label="item.label"
            :title="item.label"
          >
            <svg
              v-if="item.icon === 'chart'"
              class="sidebar-icon"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path d="M4 19V9M10 19V5M16 19v-7M3 19h18" />
            </svg>
            <svg
              v-else-if="item.icon === 'wallet'"
              class="sidebar-icon"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <rect x="3" y="6" width="18" height="13" rx="2" />
              <path d="M7 6V4h10v2M3 10h18M16 14h2" />
            </svg>
            <svg
              v-else-if="item.icon === 'people'"
              class="sidebar-icon"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <circle cx="9" cy="8" r="3" />
              <path
                d="M3.5 19c.5-3.5 2.3-5.5 5.5-5.5s5 2 5.5 5.5M16 8.5h4M18 6.5v4"
              />
            </svg>
            <svg
              v-else-if="item.icon === 'box'"
              class="sidebar-icon"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path d="m4 7 8-4 8 4-8 4zM4 7v10l8 4 8-4V7M12 11v10" />
            </svg>
            <svg
              v-else
              class="sidebar-icon"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6" />
            </svg>
            <span class="module-link-label">{{ item.label }}</span>
          </RouterLink>
        </nav>

        <DashboardMonthPicker
          v-if="authenticated && !sidebarCollapsed"
          :periods="periods"
          :selected-period="selectedPeriod"
          @select="selectPeriod"
        />

        <div class="sidebar-footer">
          <button class="theme-button" type="button" :aria-expanded="showSecurity" aria-label="负责人身份" title="负责人身份" @click="showSecurity = !showSecurity"><svg class="sidebar-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3"/><path d="M5 21v-3a7 7 0 0 1 14 0v3"/></svg><span class="control-label">负责人身份</span></button>
          <button v-if="authenticated && currentCompany" class="theme-button" type="button" :aria-expanded="showJobs" aria-label="文件与处理进度" title="文件与处理进度" @click="showJobs = !showJobs"><svg class="sidebar-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/></svg><span class="control-label">文件与处理进度</span></button>
          <button
            class="theme-button"
            type="button"
            :aria-label="theme === 'dark' ? '切换浅色外观' : '切换深色外观'"
            :aria-pressed="theme === 'dark'"
            :title="theme === 'dark' ? '切换浅色外观' : '切换深色外观'"
            @click="toggleTheme"
          >
            <svg
              v-if="theme === 'light'"
              class="sidebar-icon"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path d="M20 15.2A8 8 0 0 1 8.8 4 8 8 0 1 0 20 15.2Z" />
            </svg>
            <svg
              v-else
              class="sidebar-icon"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="3.5" />
              <path
                d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"
              />
            </svg>
            <span class="control-label">{{
              theme === "dark" ? "浅色外观" : "深色外观"
            }}</span>
          </button>
          <small class="app-version">{{ appVersion }}</small>
        </div>
      </div>
    </header>

    <main id="workspace-content" class="workspace-main" tabindex="-1">
      <OwnerSessionPanel :authenticated="authenticated" :expanded="!authenticated || showSecurity" :launch-error="launchError" @authenticated="setAuthenticated" />
      <div v-if="authenticated && contextError" class="panel" role="alert"><p>{{ contextError }}</p><button class="dashboard-action" @click="loadCompanyContext">重新读取</button></div>
      <p v-else-if="authenticated && contextLoading && !currentCompany" class="panel" role="status">正在读取所选公司的资料…</p>
      <p v-else-if="authenticated && context && !currentCompany" class="panel">还没有添加公司。添加公司后，可在这里查看财务情况。</p>
      <template v-if="authenticated && currentCompany && context?.current_company?.company_id === route.query.company_id">
        <BackgroundJobsPanel v-if="showJobs" :company-id="currentCompany.company_id" />
        <RouterView :key="currentCompany.company_id" />
      </template>
    </main>
    <div class="selection-toast-region" role="status" aria-live="polite" aria-atomic="true">
      <div v-if="authenticated && selectionNotice" class="selection-toast">
        <span>{{ selectionNotice }}</span>
        <button type="button" aria-label="关闭期间提示" @click="setSelectionNotice('')">×</button>
      </div>
    </div>
  </div>
</template>
