// One traversal supplies both display rows and the unique group count used by
// automatic paging. Exact locator rows participate without duplicating a page.
export function briefCategoryIndex<T extends { group_key: string }>(items: T[], categoryKey: (item: T) => string, focused?: T | null) {
  const categories = new Map<string, { items: T[]; groupKeys: Set<string> }>();
  const loaded = new Set<string>();
  function add(item: T) {
    const key = categoryKey(item);
    let category = categories.get(key);
    if (!category) { category = { items: [], groupKeys: new Set() }; categories.set(key, category); }
    category.items.push(item); category.groupKeys.add(item.group_key); loaded.add(item.group_key);
  }
  for (const item of items) add(item);
  if (focused && !loaded.has(focused.group_key)) {
    const key = categoryKey(focused);
    let category = categories.get(key);
    if (!category) { category = { items: [], groupKeys: new Set() }; categories.set(key, category); }
    category.items.unshift(focused); category.groupKeys.add(focused.group_key);
  }
  return categories;
}
