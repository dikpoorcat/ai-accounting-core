<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { localSecurity, localErrorMessage, type LocalSecurityState, type SecurityAction } from "../api/localKernel";

const props = defineProps<{ authenticated: boolean; expanded: boolean; launchError?: string }>();
const emit = defineEmits<{ authenticated: [value: boolean] }>();
const security = ref<LocalSecurityState>({});
const loginName = ref("");
const busy = ref(false);
const message = ref("");
const error = ref(props.launchError ?? "");
let timer: ReturnType<typeof setTimeout> | undefined;
let stopped = false;

async function update(result: LocalSecurityState): Promise<void> {
  if (stopped) return;
  security.value = { ...security.value, ...result };
  if (result.status && ["starting", "waiting_for_user", "running"].includes(result.status)) {
    message.value = "请在已打开的本机安全窗口中完成操作。";
    timer = setTimeout(() => { void poll(); }, 1200);
    return;
  }
  busy.value = false;
  if (result.status === "succeeded") {
    security.value = await localSecurity("session_status");
    emit("authenticated", security.value.authenticated === true);
    message.value = security.value.authenticated ? "负责人已登录，可以查看公司账务。" : "安全操作已完成，请登录后查看账务。";
  } else if (result.status === "failed" || result.status === "expired") {
    error.value = result.status === "expired" ? "安全窗口操作已超时，请重新发起。" : "安全窗口未完成操作，请查看本机窗口提示后重试。";
    message.value = "";
  } else if (result.status === "cancelled") message.value = "已取消安全窗口操作。";
}
async function poll() {
  try { await update(await localSecurity("status", { request_id: security.value.request_id })); }
  catch (caught) { busy.value = false; error.value = localErrorMessage(caught); }
}
async function request(kind: SecurityAction) {
  clearTimeout(timer);
  busy.value = true; error.value = ""; message.value = "正在打开本机安全窗口…";
  try { await update(await localSecurity("request", { kind, ...(kind === "bootstrap_owner" ? { login_name: loginName.value.trim() } : {}) })); }
  catch (caught) { busy.value = false; message.value = ""; error.value = localErrorMessage(caught); }
}
async function cancel() {
  clearTimeout(timer);
  try { await update(await localSecurity("cancel", { request_id: security.value.request_id })); }
  catch (caught) { busy.value = false; error.value = localErrorMessage(caught); }
}
onMounted(async () => {
  try {
    security.value = await localSecurity("session_status");
    if (!stopped) emit("authenticated", security.value.authenticated === true);
  } catch (caught) { error.value = localErrorMessage(caught); }
});
onBeforeUnmount(() => { stopped = true; clearTimeout(timer); });
</script>

<template>
  <section v-show="expanded" class="session-panel panel" aria-labelledby="owner-heading" :aria-busy="busy">
    <h2 id="owner-heading">负责人身份 <small>{{ authenticated ? "本页已登录" : "本页未登录" }}</small></h2>
    <p>密码与恢复码只在本机安全窗口输入。</p>
    <div class="session-controls">
      <template v-if="security.provisioned === false">
        <label>负责人登录名<input v-model="loginName" autocomplete="username" maxlength="100" :disabled="busy"></label>
        <button class="dashboard-action" :disabled="busy || !loginName.trim()" @click="request('bootstrap_owner')">设置负责人</button>
      </template>
      <template v-else>
        <button class="dashboard-action" :disabled="busy" @click="request('login')">负责人登录</button>
        <button class="dashboard-action" :disabled="busy" @click="request('change_password')">修改密码</button>
        <button class="dashboard-action" :disabled="busy" @click="request('recover')">恢复访问</button>
        <button class="dashboard-action" :disabled="busy" @click="request('replace_recovery_code')">更换恢复码</button>
      </template>
      <button v-if="busy && security.request_id" class="dashboard-action" @click="cancel">取消操作</button>
    </div>
    <p v-if="message" role="status">{{ message }}</p><p v-if="error" class="session-error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.session-panel { padding: 22px; margin-bottom: 22px; }
h2 { margin: 0; font-size: 19px; } h2 small, p { color: var(--muted); font-size: 13px; }
.session-controls { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; }
label { display: grid; gap: 6px; font-size: 13px; } input { min-height: 38px; border: 1px solid var(--line); border-radius: 8px; padding: 8px; background: var(--surface); color: var(--text); }
.session-error { color: var(--danger); }
</style>
