import type { BriefOpenItem, BriefOpenItems } from "../api/brief";

export type ContributionComponent = NonNullable<BriefOpenItem["contribution_component"]>;
export type BriefOpenRow = BriefOpenItem & { contributionMembers?: BriefOpenItem[]; contributionNotices?: string[] };
type OpenStatus = BriefOpenItem["status"];

export const contributionComponents: { key: ContributionComponent; label: string }[] = [
  { key: "employee_social", label: "个人社保" },
  { key: "employer_social", label: "公司社保" },
  { key: "employee_housing", label: "个人公积金" },
  { key: "employer_housing", label: "公司公积金" },
];

function sum(items: BriefOpenItem[], field: "source_amount_fen" | "paid_fen" | "other_settled_fen" | "outstanding_fen" | "current_outstanding_fen"): string | null {
  let total = 0n;
  for (const item of items) {
    const value = item[field];
    if (value == null) return null;
    total += BigInt(value);
  }
  return total.toString();
}

function combinedStatus(items: BriefOpenItem[], current = false): OpenStatus {
  if (!items.length) return "checking";
  const states = items.map(item => current ? item.current_status : item.status);
  const balances = items.map(item => current ? item.current_outstanding_fen : item.outstanding_fen);
  if (states.includes("over_settled") || balances.some(value => value != null && BigInt(value) < 0n)) return "over_settled";
  if (states.some(value => value == null || !["open", "partial", "settled", "reversed", "withdrawn"].includes(value)) || balances.some(value => value == null)) return "checking";
  if (states.some(value => value === "open" || value === "partial")) {
    if (states.some(value => value === "partial" || value === "settled") || (!current && items.some(item =>
      (item.paid_fen != null && BigInt(item.paid_fen) !== 0n) || (item.other_settled_fen != null && BigInt(item.other_settled_fen) !== 0n)))) return "partial";
    return "open";
  }
  if (states.includes("reversed")) return "reversed";
  if (states.includes("withdrawn")) return "withdrawn";
  if (states.every(value => value === "settled") && balances.every(value => value != null && BigInt(value) === 0n)) return "settled";
  return "checking";
}

function settlementNotices(items: BriefOpenItem[], status: OpenStatus, current = false): string[] {
  const states = items.map(item => current ? item.current_status : item.status);
  const notices: string[] = [];
  if (status !== "reversed" && states.includes("reversed")) notices.push("含已更正款项");
  if (status !== "withdrawn" && states.includes("withdrawn")) notices.push("含已撤回款项");
  return notices;
}

export function payrollMonthLabel(period: string) {
  const [year, month] = period.split("-");
  return `${year}年${Number(month)}月`;
}

type OpenItemDisplay = { items: BriefOpenRow[]; summary: BriefOpenItems };

export function createBriefOpenItemGrouping(makeRows: () => BriefOpenRow[] = () => []) {
  let source: BriefOpenItem[] | null = null, loaded = 0;
  let rows = makeRows();
  const groups = new Map<string, BriefOpenRow>();
  let previousSummary: BriefOpenItems | null = null, previousComplete = false;
  let result: OpenItemDisplay | null = null;
  return (summary: BriefOpenItems, items: BriefOpenItem[], complete: boolean): OpenItemDisplay => {
    // A new raw collection is a new read scope; continuations append to the same array.
    if (source !== items || items.length < loaded) {
      source = items; loaded = 0; rows = makeRows(); groups.clear(); result = null;
    }
    if (result && loaded === items.length && previousSummary === summary && previousComplete === complete) return result;
    for (let index = loaded; index < items.length; index++) {
      const item = items[index]!;
      if (item.category_key !== "payroll_payables" || !item.contribution_group_key || !item.payroll_period
        || !contributionComponents.some(component => component.key === item.contribution_component)) {
        rows.push(item);
        continue;
      }
      // Both fields are formal metadata; the month also guards inconsistent response metadata.
      const key = JSON.stringify([item.contribution_group_key, item.payroll_period]);
      const existing = groups.get(key);
      if (existing) existing.contributionMembers!.push(item);
      else {
        const row: BriefOpenRow = { ...item, id: `contribution:${key}`, subject_id: null,
          description: "社保与公积金", contributionMembers: [item],
          source_amount_fen: null, paid_fen: null, other_settled_fen: null, outstanding_fen: null,
          current_outstanding_fen: null, current_status: null };
        groups.set(key, row); rows.push(row);
      }
    }
    loaded = items.length;
    previousSummary = summary; previousComplete = complete;
    result = finishGrouping(summary, rows, groups, complete);
    return result;
  };
}

function finishGrouping(summary: BriefOpenItems, rows: BriefOpenRow[], groups: Map<string, BriefOpenRow>, complete: boolean): OpenItemDisplay {
  if (!complete) return { items: rows, summary };
  for (const row of groups.values()) {
    const members = row.contributionMembers!;
    row.source_amount_fen = sum(members, "source_amount_fen");
    row.paid_fen = sum(members, "paid_fen");
    row.other_settled_fen = sum(members, "other_settled_fen");
    row.outstanding_fen = sum(members, "outstanding_fen");
    row.current_outstanding_fen = sum(members, "current_outstanding_fen");
    row.status = combinedStatus(members);
    row.current_status = combinedStatus(members, true);
    row.contributionNotices = settlementNotices(members, row.current_status, true);
  }
  const categoryCounts = new Map<string, number>();
  for (const row of rows) categoryCounts.set(row.category_key, (categoryCounts.get(row.category_key) ?? 0) + 1);
  const categories = summary.categories.map(category => {
    const count = categoryCounts.get(category.key) ?? 0;
    return { ...category, count, loaded_count: count };
  });
  const receivableCount = categories.filter(category => category.direction === "receivable").reduce((count, category) => count + category.count, 0);
  const payableCount = categories.filter(category => category.direction === "payable").reduce((count, category) => count + category.count, 0);
  return { items: rows, summary: { ...summary, categories, receivable_count: receivableCount, payable_count: payableCount, total_count: rows.length } };
}

export function groupBriefOpenItems(summary: BriefOpenItems, items: BriefOpenItem[], complete: boolean): OpenItemDisplay {
  return createBriefOpenItemGrouping()(summary, items, complete);
}

export interface ContributionProgressRow {
  component: ContributionComponent;
  label: string;
  present: boolean;
  sourceAmountFen: string | null;
  paidFen: string | null;
  otherSettledFen: string | null;
  outstandingFen: string | null;
  status: OpenStatus;
  currentOutstandingFen: string | null;
  currentStatus: OpenStatus;
  notices: string[];
  currentNotices: string[];
  changed: boolean;
}

export function contributionProgressRows(item: BriefOpenRow): ContributionProgressRow[] {
  return contributionComponents.map(component => {
    const members = item.contributionMembers?.filter(member => member.contribution_component === component.key) ?? [];
    const present = members.length > 0;
    const status = combinedStatus(members), currentStatus = combinedStatus(members, true);
    return { component: component.key, label: component.label, present,
      sourceAmountFen: present ? sum(members, "source_amount_fen") : null,
      paidFen: present ? sum(members, "paid_fen") : null,
      otherSettledFen: present ? sum(members, "other_settled_fen") : null,
      outstandingFen: present ? sum(members, "outstanding_fen") : null,
      status,
      currentOutstandingFen: present ? sum(members, "current_outstanding_fen") : null,
      currentStatus, notices: settlementNotices(members, status), currentNotices: settlementNotices(members, currentStatus, true),
      changed: members.some(member => member.current_status != null &&
        (member.current_status !== member.status || member.current_outstanding_fen !== member.outstanding_fen)),
    };
  });
}
