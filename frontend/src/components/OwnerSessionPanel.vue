<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { isSecurityRequestState, isSecuritySessionStatus, localSecurity, localErrorMessage, type LocalSecurityState, type SecurityAction } from "../api/localKernel";

const props = defineProps<{ authenticated: boolean; expanded: boolean; launchError?: string }>();
const emit = defineEmits<{ authenticated: [value: boolean]; close: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const security = ref<LocalSecurityState | null>(null);
const busy = ref(false);
const message = ref("");
const error = ref(props.launchError ?? "");
let timer: ReturnType<typeof setTimeout> | undefined;
let stopped = false;
let previousOverflow: string | undefined;

function syncDialog() {
  if (!dialog.value) return;
  if (props.expanded) {
    if (!dialog.value.open) dialog.value.showModal();
    if (previousOverflow === undefined) {
      previousOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }
  } else {
    dialog.value.close();
    restoreScrolling();
  }
}
function restoreScrolling() {
  if (previousOverflow === undefined) return;
  document.body.style.overflow = previousOverflow;
  previousOverflow = undefined;
}
function dismissBackdrop(event: MouseEvent) {
  const bounds = dialog.value?.getBoundingClientRect();
  if (bounds && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) emit("close");
}
watch(() => props.expanded, syncDialog, { flush: "post" });

async function update(result: LocalSecurityState): Promise<void> {
  if (stopped) return;
  security.value = result;
  if (isSecurityRequestState(result) && ["starting", "waiting_for_user", "running"].includes(result.status)) {
    message.value = "请在已打开的本机安全窗口中完成操作。";
    timer = setTimeout(() => { void poll(); }, 1200);
    return;
  }
  busy.value = false;
  if (isSecurityRequestState(result) && result.status === "succeeded") {
    const session = await localSecurity("session_status");
    if (stopped) return;
    security.value = session;
    const authenticated = isSecuritySessionStatus(session) && session.authenticated;
    emit("authenticated", authenticated);
    message.value = authenticated ? "负责人已登录，可以查看公司账务。" : "安全操作已完成，请登录后查看账务。";
  } else if (isSecurityRequestState(result) && ["failed", "expired", "cancelled"].includes(result.status)) {
    const session = await localSecurity("session_status");
    if (stopped) return;
    security.value = session;
    emit("authenticated", isSecuritySessionStatus(session) && session.authenticated);
    if (result.status === "cancelled") message.value = "已取消安全窗口操作。";
    else {
      error.value = result.status === "expired" ? "安全窗口操作已超时，请重新发起。" : "安全窗口未完成操作，请查看本机窗口提示后重试。";
      message.value = "";
    }
  }
}
async function poll() {
  if (!security.value || !isSecurityRequestState(security.value)) return;
  try { await update(await localSecurity("status", { request_id: security.value.request_id })); }
  catch (caught) { busy.value = false; error.value = localErrorMessage(caught); }
}
async function request(kind: SecurityAction) {
  clearTimeout(timer);
  busy.value = true; error.value = ""; message.value = "正在打开本机安全窗口…";
  try { await update(await localSecurity("request", { kind })); }
  catch (caught) { busy.value = false; message.value = ""; error.value = localErrorMessage(caught); }
}
async function cancel() {
  clearTimeout(timer);
  if (!security.value || !isSecurityRequestState(security.value)) return;
  try { await update(await localSecurity("cancel", { request_id: security.value.request_id })); }
  catch (caught) { busy.value = false; error.value = localErrorMessage(caught); }
}
onMounted(async () => {
  syncDialog();
  try {
    const session = await localSecurity("session_status");
    security.value = session;
    if (!stopped) emit("authenticated", isSecuritySessionStatus(session) && session.authenticated);
  } catch (caught) {
    error.value = localErrorMessage(caught);
    if (!stopped) emit("authenticated", false);
  }
});
onBeforeUnmount(() => { stopped = true; clearTimeout(timer); dialog.value?.close(); restoreScrolling(); });
</script>

<template>
  <dialog ref="dialog" id="owner-dialog" class="session-dialog" aria-labelledby="owner-heading" aria-describedby="owner-description" :aria-busy="busy" @cancel.prevent="emit('close')" @click.self="dismissBackdrop">
    <header class="dialog-heading">
      <div>
        <h2 id="owner-heading">负责人身份</h2>
        <span class="session-state" :class="{ authenticated }">{{ authenticated ? "已登录" : "未登录" }}</span>
      </div>
      <button class="close-button" type="button" aria-label="关闭负责人身份" autofocus @click="emit('close')">×</button>
    </header>
    <p id="owner-description" class="session-note">负责人设置、密码与恢复码只在本机安全窗口处理。</p>
    <p v-if="security && isSecuritySessionStatus(security) && security.login_name" class="login-name"><span>负责人登录名</span><strong>{{ security.login_name }}</strong></p>

    <div v-if="security && isSecuritySessionStatus(security) && security.provisioned === false" class="setup-controls">
      <button class="dashboard-action primary-action" type="button" :disabled="busy" @click="request('bootstrap_owner')">设置负责人</button>
    </div>
    <template v-else>
      <button v-if="!authenticated" class="dashboard-action primary-action" type="button" :disabled="busy" @click="request('login')">负责人登录</button>
      <div class="security-actions" aria-label="账号与安全">
        <button type="button" :disabled="busy" @click="request('change_password')"><span><strong>修改密码</strong><small>更换负责人登录密码</small></span><span aria-hidden="true">›</span></button>
        <button type="button" :disabled="busy" @click="request('recover')"><span><strong>恢复访问</strong><small>忘记密码时使用恢复码</small></span><span aria-hidden="true">›</span></button>
        <button type="button" :disabled="busy" @click="request('replace_recovery_code')"><span><strong>更换恢复码</strong><small>生成新的恢复码</small></span><span aria-hidden="true">›</span></button>
      </div>
    </template>
    <div v-if="message || error || busy" class="session-feedback">
      <p v-if="message" role="status">{{ message }}</p>
      <p v-if="error" class="session-error" role="alert">{{ error }}</p>
      <button v-if="busy && security && isSecurityRequestState(security)" class="dashboard-action" type="button" @click="cancel">取消操作</button>
    </div>
  </dialog>
</template>

<style scoped>
.session-dialog { width: min(480px, calc(100% - 32px)); max-height: calc(100dvh - 32px); margin: auto; padding: 24px; overflow-y: auto; border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface); color: var(--text); box-shadow: var(--shadow-overlay); }
.session-dialog::backdrop { background: rgb(12 24 17 / 38%); }
.dialog-heading, .dialog-heading > div { display: flex; align-items: center; gap: 12px; }
.dialog-heading { justify-content: space-between; }
h2 { margin: 0; font-size: 20px; }
.session-state { padding: 2px 8px; border-radius: 999px; background: var(--surface-soft); color: var(--muted); font-size: 11px; white-space: nowrap; }
.session-state.authenticated { background: var(--accent-soft); color: var(--accent); }
.close-button { display: grid; width: 32px; height: 32px; flex: none; place-items: center; padding: 0; border: 0; border-radius: var(--radius-control); background: var(--surface-soft); color: var(--muted); font-size: 24px; line-height: 1; cursor: pointer; }
.close-button:hover { background: var(--accent-soft); color: var(--accent); }
.session-note { margin: 12px 0 20px; color: var(--muted); font-size: 13px; }
.login-name { display: grid; gap: 4px; margin: 0 0 20px; }
.login-name span { color: var(--muted); font-size: 12px; }
.login-name strong { overflow-wrap: anywhere; font-size: 16px; }
.primary-action { width: 100%; margin-bottom: 16px; background: var(--accent); color: var(--surface); }
.security-actions { overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius-control); }
.security-actions button { display: flex; width: 100%; align-items: center; justify-content: space-between; gap: 16px; padding: 13px 15px; border: 0; background: var(--surface); text-align: left; cursor: pointer; }
.security-actions button + button { border-top: 1px solid var(--line); }
.security-actions button:hover:not(:disabled) { background: var(--surface-soft); }
.security-actions button > span:first-child { display: grid; gap: 3px; min-width: 0; }
.security-actions strong { font-size: 14px; font-weight: 650; }
.security-actions small { color: var(--muted); font-size: 12px; }
.security-actions button > span:last-child { color: var(--muted); font-size: 22px; }
.security-actions button:focus-visible { outline-offset: -3px; }
button:disabled { opacity: .55; cursor: wait; }
.setup-controls { display: grid; gap: 14px; }
.session-feedback { display: grid; justify-items: start; gap: 12px; margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--line); }
.session-feedback p { margin: 0; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.session-feedback .session-error { color: var(--danger); }
@media (max-width: 480px) { .session-dialog { padding: 20px; } .dialog-heading > div { gap: 8px; } }
</style>
