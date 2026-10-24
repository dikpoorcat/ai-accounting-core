import { requestGeneratedJson } from "./client";
import { LocalApiError } from "./localKernel";
import { validateCompanyNoteResponse } from "./generated/companyNote.js";
import { validateCompanyNoteQuery } from "./generated/companyNoteQuery.js";
import type { CompanyNoteResponse } from "./generated/companyNote";

export type { CompanyNoteResponse };

export async function fetchCompanyNote(companyId: string, signal?: AbortSignal): Promise<CompanyNoteResponse> {
  const query = { company_id: companyId };
  if (!validateCompanyNoteQuery(query)) {
    throw new LocalApiError(400, "invalid_input", "请先选择要查看的公司。");
  }
  const endpoint = "/api/dashboard/company-note";
  const response = await requestGeneratedJson(
    `${endpoint}?${new URLSearchParams(query)}`, endpoint, validateCompanyNoteResponse,
    (url, value) => url.searchParams.get("company_id") === companyId && value.company.company_id === companyId,
    { signal },
  );
  signal?.throwIfAborted();
  return response;
}
