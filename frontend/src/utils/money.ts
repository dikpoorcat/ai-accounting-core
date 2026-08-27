const integerFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 0,
});

export function fen(value: string | number | bigint | null | undefined): bigint {
  if (value === null || value === undefined || value === "") return 0n;
  return BigInt(value);
}

export function formatFen(
  value: string | number | bigint | null | undefined,
): string {
  const amount = fen(value);
  const negative = amount < 0n;
  const absolute = negative ? -amount : amount;
  const yuan = absolute / 100n;
  const cents = String(absolute % 100n).padStart(2, "0");
  return `${negative ? "−" : ""}¥${integerFormatter.format(yuan)}.${cents}`;
}

export function formatPositiveFen(
  value: string | number | bigint | null | undefined,
): string {
  const amount = fen(value);
  return formatFen(amount < 0n ? -amount : amount);
}
