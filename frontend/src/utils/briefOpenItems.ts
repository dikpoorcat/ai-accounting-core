import type { BriefOpenItem } from "../api/brief";

export type ContributionComponent = NonNullable<BriefOpenItem["contribution_component"]>;
export type BriefOpenRow = BriefOpenItem & { contributionMembers?: BriefOpenItem[]; contributionNotices?: string[]; contributionMatter: "social" | "housing" };
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

// This prepares the two components of each contribution matter within an already read group.
// Root display groups and their totals always come from the server.
export function groupContributionMembers(items: BriefOpenItem[]): BriefOpenRow[] {
  const groups = new Map<string, BriefOpenRow>();
  for (const item of items) {
    if (item.category_key !== "payroll_payables" || !item.contribution_group_key || !item.payroll_period
      || !contributionComponents.some(component => component.key === item.contribution_component)) continue;
    const matter = item.contribution_component!.endsWith("_social") ? "social" : "housing";
    const key = JSON.stringify([item.contribution_group_key, item.payroll_period, matter]);
    const group = groups.get(key);
    if (group) group.contributionMembers!.push(item);
    else groups.set(key, { ...item, id: `contribution:${key}`, subject_id: null, contributionMatter: matter, contributionMembers: [item] });
  }
  return [...groups.values()];
}

export interface ContributionProgressRow {
  component: ContributionComponent;
  label: string;
  members: BriefOpenItem[];
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
  return contributionComponents.filter(component => component.key.endsWith(`_${item.contributionMatter}`)).map(component => {
    const members = item.contributionMembers?.filter(member => member.contribution_component === component.key) ?? [];
    const present = members.length > 0;
    const status = combinedStatus(members), currentStatus = combinedStatus(members, true);
    return { component: component.key, label: component.label, members, present,
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
