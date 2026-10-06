// Call only after the page's scope, snapshot and request identity have been checked.
export function appendDashboardCollection<T extends { items: unknown[] }>(current: T, next: T): T {
  for (const item of next.items) current.items.push(item);
  return { ...next, items: current.items };
}
