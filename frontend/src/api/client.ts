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

export async function requestJson<T>(
  path: string,
  options: { signal?: AbortSignal } = {},
): Promise<T> {
  const response = await fetch(withCurrentCompany(path), {
    headers: { Accept: "application/json" },
    signal: options.signal,
  });
  const payload = (await response.json().catch(() => null)) as
    | Record<string, unknown>
    | null;
  if (!response.ok) {
    const errors = Array.isArray(payload?.errors) ? payload.errors : [];
    const code = typeof errors[0] === "string" ? errors[0] : "DASHBOARD_REQUEST_FAILED";
    const message =
      typeof payload?.message === "string"
        ? payload.message
        : "财务工作台数据加载失败，请稍后重试。";
    throw new DashboardApiError(response.status, code, message);
  }
  return payload as T;
}

export function withCurrentCompany(path: string): string {
  const target = new URL(path, window.location.origin);
  const current = new URLSearchParams(window.location.search).get("org_id");
  if (current && !target.searchParams.has("org_id")) {
    target.searchParams.set("org_id", current);
  }
  return `${target.pathname}${target.search}${target.hash}`;
}

export function dashboardErrorMessage(error: unknown): string {
  if (error instanceof DashboardApiError) return error.message;
  if (error instanceof DOMException && error.name === "AbortError") return "";
  return "财务工作台数据加载失败，请稍后重试。";
}
