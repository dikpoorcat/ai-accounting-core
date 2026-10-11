import { shallowReactive } from "vue";

// Call only after the page's scope, snapshot and request identity have been checked.
export function appendDashboardCollection<T extends { items: unknown[] }>(current: T, next: T): T {
  for (const item of next.items) current.items.push(item);
  return { ...next, items: current.items };
}
// HTTP validators have already checked these collections. The backend rows are
// immutable in the read-only dashboard; only the containing arrays are appended.
export function prepareDashboardCollections(data: { collections: object } | null) {
  if (!data) return;
  for (const collection of Object.values(data.collections)) {
    if (collection) collection.items = shallowReactive(collection.items);
  }
}
