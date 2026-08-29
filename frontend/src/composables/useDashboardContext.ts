import { readonly, ref } from "vue";

import { dashboardErrorMessage } from "../api/client";
import { fetchDashboardContext, type DashboardContext } from "../api/context";

const context = ref<DashboardContext | null>(null);
const loading = ref(false);
const error = ref("");
let pending: Promise<DashboardContext> | null = null;
let controller: AbortController | null = null;
let requestVersion = 0;

async function load(force = false): Promise<DashboardContext> {
  if (context.value && !force) return context.value;
  if (pending && !force) return pending;
  loading.value = true;
  error.value = "";
  controller?.abort();
  controller = new AbortController();
  const version = ++requestVersion;
  if (force) context.value = null;
  pending = fetchDashboardContext(controller.signal)
    .then((value) => {
      if (version === requestVersion) context.value = value;
      return value;
    })
    .catch((caught: unknown) => {
      if (version === requestVersion) error.value = dashboardErrorMessage(caught);
      throw caught;
    })
    .finally(() => {
      if (version === requestVersion) {
        loading.value = false;
        pending = null;
        controller = null;
      }
    });
  return pending;
}

function cancel() {
  requestVersion += 1;
  controller?.abort();
  controller = null;
  pending = null;
  context.value = null;
  loading.value = false;
}

export function useDashboardContext() {
  return {
    context: readonly(context),
    loading: readonly(loading),
    error: readonly(error),
    load,
    cancel,
  };
}
