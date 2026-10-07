async function selectDashboardOption(page, label, value) {
  if (label === "切换公司") {
    await page.getByRole("combobox", { name: label, exact: true }).selectOption(value);
    return;
  }
  const trigger = page.getByRole("button", { name: label, exact: true });
  await trigger.scrollIntoViewIfNeeded();
  // Settle viewport scrolling before opening: the anchored menu correctly
  // dismisses itself on a document scroll.
  await trigger.evaluate(node => new Promise(resolve => {
    let previous = `${scrollX}:${scrollY}`, stable = 0;
    const settle = () => {
      const current = `${scrollX}:${scrollY}`;
      stable = current === previous ? stable + 1 : 0; previous = current;
      if (stable >= 3) { node.focus({ preventScroll: true }); resolve(); }
      else requestAnimationFrame(settle);
    };
    requestAnimationFrame(settle);
  }));
  await page.keyboard.press("ArrowDown");
  const id = await trigger.getAttribute("aria-controls");
  if (!id) throw new Error(`Selection menu did not open: ${label}`);
  const menu = page.locator(`[role="listbox"][id=${JSON.stringify(id)}]`);
  const option = menu.locator(`[role="option"][data-value=${JSON.stringify(value)}]`);
  await option.waitFor({ state: "visible" });
  // Locator.press also focuses and may scroll the document. Focus explicitly
  // without scrolling, then use a real keyboard action on the visible option.
  await option.evaluate(node => node.focus({ preventScroll: true }));
  await page.keyboard.press("Enter");
}

async function readDashboardValue(page, label) {
  if (label === "切换公司") {
    return page.getByRole("combobox", { name: label, exact: true }).inputValue();
  }
  return page.getByRole("button", { name: label, exact: true }).getAttribute("data-value");
}

module.exports = { selectDashboardOption, readDashboardValue };
