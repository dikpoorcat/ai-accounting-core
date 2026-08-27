import { readonly, ref } from "vue";

import { dashboardErrorMessage } from "../api/client";
import { fetchDashboardContext, type DashboardContext } from "../api/context";

const context = ref<DashboardContext | null>(null);
const loading = ref(false);
const error = ref("");
let pending: Promise<DashboardContext> | null = null;

async function load(force = false): Promise<DashboardContext> {
  if (context.value && !force) return context.value;
  if (pending && !force) return pending;
  loading.value = true;
  error.value = "";
  pending = fetchDashboardContext()
    .then((value) => {
      context.value = value;
      return value;
    })
    .catch((caught: unknown) => {
      error.value = dashboardErrorMessage(caught);
      throw caught;
    })
    .finally(() => {
      loading.value = false;
      pending = null;
    });
  return pending;
}

export function useDashboardContext() {
  return {
    context: readonly(context),
    loading: readonly(loading),
    error: readonly(error),
    load,
  };
}
