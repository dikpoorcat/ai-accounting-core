import { LocalApiError, requestLocalJson } from "./localKernel";
import type { DashboardResponseValidator } from "./generated/dashboardValidatorTypes";
import type { DashboardContextResponse } from "./generated/dashboardContext";
import { validateDashboardContextResponse } from "./generated/dashboardContext.js";

export class DashboardApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "DashboardApiError";
    this.status = status;
    this.code = code;
  }
}

export async function requestGeneratedJson<T>(
  path: string,
  endpoint: string,
  validator: (value: unknown) => value is T,
  matchesRequest: (url: URL, value: T) => boolean,
  options: { signal?: AbortSignal } = {},
): Promise<T> {
  const target = withCurrentCompany(path);
  const payload = await requestLocalJson(target, { signal: options.signal });
  const url = new URL(target, window.location.origin);
  if (url.pathname !== endpoint || !validator(payload) || !matchesRequest(url, payload)) {
    throw new DashboardApiError(502, "DASHBOARD_SCHEMA_MISMATCH", "看板数据格式不匹配，请更新本地内核后重试。");
  }
  return payload;
}

export type DashboardDeliveryAttempt<T> =
  | { state: "ready"; response: T; modelVersion: string | null }
  | { state: "preparing"; retryAfterMs: number }
  | { state: "failed"; reason: "corrupt" | "production_failed" };

export async function requestGeneratedDashboard<T, TDelivery>(
  path: string,
  endpoint: string,
  deliveryValidator: DashboardResponseValidator<TDelivery>,
  inspectDelivery: (delivery: TDelivery) => DashboardDeliveryAttempt<T>,
  matchesDelivery: (url: URL, delivery: TDelivery) => boolean,
  responseValidator: DashboardResponseValidator<T>,
  matchesResponse: (url: URL, response: T) => boolean,
  options: { signal?: AbortSignal; onPreparing?: () => void; retry?: boolean } = {},
): Promise<T> {
  const target = withCurrentCompany(path);
  const url = new URL(target, window.location.origin);
  if (url.pathname !== endpoint) {
    throw new DashboardApiError(502, "DASHBOARD_SCHEMA_MISMATCH", "看板数据格式不匹配，请更新本地内核后重试。");
  }

  let retry = options.retry === true;
  while (true) {
    options.signal?.throwIfAborted();
    const payload = await requestLocalJson(retry ? `${url.pathname}/retry` : target, {
      signal: options.signal,
      ...(retry ? { method: "POST", body: JSON.stringify(Object.fromEntries(url.searchParams)) } : {}),
    });
    retry = false;
    options.signal?.throwIfAborted();
    if (!deliveryValidator(payload) || !matchesDelivery(url, payload)) {
      throw new DashboardApiError(502, "DASHBOARD_SCHEMA_MISMATCH", "看板数据格式不匹配，请更新本地内核后重试。");
    }

    const attempt = inspectDelivery(payload);
    if (attempt.state === "preparing") {
      options.onPreparing?.();
      await waitForDashboardRetry(attempt.retryAfterMs, options.signal);
      continue;
    }
    if (attempt.state === "failed") {
      const message = attempt.reason === "corrupt"
        ? "看板数据暂时无法读取，请刷新重试。"
        : "看板暂时无法更新，请稍后重试。";
      throw new DashboardApiError(503, `DASHBOARD_${attempt.reason.toUpperCase()}`, message);
    }
    if (!responseValidator(attempt.response) || !matchesResponse(url, attempt.response)) {
      throw new DashboardApiError(502, "DASHBOARD_SCHEMA_MISMATCH", "看板数据格式不匹配，请更新本地内核后重试。");
    }
    return attempt.response;
  }
}

function waitForDashboardRetry(delayMs: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const finish = () => {
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    const timer = setTimeout(finish, delayMs);
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(signal?.reason ?? new DOMException("Aborted", "AbortError"));
    };
    signal?.addEventListener("abort", abort, { once: true });
  });
}

function contextMatchesRequest(url: URL, response: DashboardContextResponse): boolean {
  const companyId = url.searchParams.get("company_id");
  return companyId === null || response.current_company?.company_id === companyId;
}

export function requestDashboardContext(options: { signal?: AbortSignal } = {}): Promise<DashboardContextResponse> {
  return requestGeneratedJson(
    "/api/dashboard/context", "/api/dashboard/context", validateDashboardContextResponse, contextMatchesRequest, options,
  );
}

export function withCurrentCompany(path: string): string {
  const target = new URL(path, window.location.origin);
  const current = new URLSearchParams(window.location.search).get("company_id");
  if (current && !target.searchParams.has("company_id")) {
    target.searchParams.set("company_id", current);
  }
  return `${target.pathname}${target.search}${target.hash}`;
}

export function dashboardErrorMessage(error: unknown): string {
  if (error instanceof DashboardApiError || error instanceof LocalApiError) return error.message;
  if (error instanceof DOMException && error.name === "AbortError") return "";
  return "财务工作台数据加载失败，请稍后重试。";
}

export function isDashboardSnapshotChanged(error: unknown): boolean {
  return (error instanceof DashboardApiError || error instanceof LocalApiError)
    && error.code === "dashboard_snapshot_changed";
}
