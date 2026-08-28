<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { RouterLink, RouterView, useRoute, useRouter } from "vue-router";

import DashboardMonthPicker from "./components/DashboardMonthPicker.vue";
import { useDashboardContext } from "./composables/useDashboardContext";

type Theme = "light" | "dark";

const appVersion = __APP_VERSION__;
const navItems = [
  { name: "brief", label: "经营简报", path: "/", icon: "chart" },
  { name: "funds", label: "资金", path: "/funds", icon: "wallet" },
  { name: "employees", label: "员工", path: "/employees", icon: "people" },
  { name: "assets", label: "资产", path: "/assets", icon: "box" },
  { name: "reports", label: "财务报表", path: "/reports", icon: "report" },
] as const;

const route = useRoute();
const router = useRouter();
const savedTheme = localStorage.getItem("finance-dashboard-theme");
const theme = ref<Theme>(savedTheme === "dark" ? "dark" : "light");
const { context, load: loadContext } = useDashboardContext();
const companyName = computed(() => context.value?.company || "只读财务工作台");
const sidebarCollapsed = ref(
  localStorage.getItem("finance-dashboard-sidebar") === "collapsed",
);
const brandInitial = computed(() => companyName.value.trim().slice(0, 1) || "财");
const periods = computed(() => context.value?.periods ?? []);
const selectedPeriod = computed(() => {
  const requested = typeof route.query.period === "string" ? route.query.period : null;
  if (periods.value.some((period) => period.key === requested)) return requested ?? "";
  const defaultPeriod = context.value?.default_period;
  if (periods.value.some((period) => period.key === defaultPeriod)) return defaultPeriod ?? "";
  return periods.value.at(-1)?.key ?? "";
});

onMounted(() => {
  void loadContext().catch(() => undefined);
});

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

async function selectPeriod(periodKey: string) {
  if (route.query.period === periodKey) return;
  await router.push({ query: { ...route.query, period: periodKey } });
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
        <svg class="sidebar-icon sidebar-toggle-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="m14 6-6 6 6 6" />
        </svg>
      </button>

      <div class="sidebar-scroll-area">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true">{{ brandInitial }}</span>
          <div class="brand-copy">
            <strong class="brand-name">{{ companyName }}</strong>
          </div>
        </div>

        <nav class="module-nav" aria-label="看板模块">
          <RouterLink
            v-for="item in navItems"
            :key="item.name"
            :to="{ path: item.path, query: route.query }"
            class="module-link"
            :aria-label="item.label"
            :title="item.label"
          >
            <svg v-if="item.icon === 'chart'" class="sidebar-icon" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 19V9M10 19V5M16 19v-7M3 19h18" />
            </svg>
            <svg v-else-if="item.icon === 'wallet'" class="sidebar-icon" viewBox="0 0 24 24" aria-hidden="true">
              <rect x="3" y="6" width="18" height="13" rx="2" />
              <path d="M7 6V4h10v2M3 10h18M16 14h2" />
            </svg>
            <svg v-else-if="item.icon === 'people'" class="sidebar-icon" viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="9" cy="8" r="3" />
              <path d="M3.5 19c.5-3.5 2.3-5.5 5.5-5.5s5 2 5.5 5.5M16 8.5h4M18 6.5v4" />
            </svg>
            <svg v-else-if="item.icon === 'box'" class="sidebar-icon" viewBox="0 0 24 24" aria-hidden="true">
              <path d="m4 7 8-4 8 4-8 4zM4 7v10l8 4 8-4V7M12 11v10" />
            </svg>
            <svg v-else class="sidebar-icon" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6" />
            </svg>
            <span class="module-link-label">{{ item.label }}</span>
          </RouterLink>
        </nav>

        <DashboardMonthPicker
          v-if="!sidebarCollapsed"
          :periods="periods"
          :selected-period="selectedPeriod"
          @select="selectPeriod"
        />

        <div class="sidebar-footer">
          <button
            class="theme-button"
            type="button"
            :aria-label="theme === 'dark' ? '切换浅色外观' : '切换深色外观'"
            :aria-pressed="theme === 'dark'"
            :title="theme === 'dark' ? '切换浅色外观' : '切换深色外观'"
            @click="toggleTheme"
          >
            <svg v-if="theme === 'light'" class="sidebar-icon" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M20 15.2A8 8 0 0 1 8.8 4 8 8 0 1 0 20 15.2Z" />
            </svg>
            <svg v-else class="sidebar-icon" viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="12" cy="12" r="3.5" />
              <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
            </svg>
            <span class="control-label">{{ theme === "dark" ? "浅色外观" : "深色外观" }}</span>
          </button>
          <small class="app-version">{{ appVersion }}</small>
        </div>
      </div>
    </header>

    <main id="workspace-content" class="workspace-main" tabindex="-1">
      <RouterView />
    </main>
  </div>
</template>
