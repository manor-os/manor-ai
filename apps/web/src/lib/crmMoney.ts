/** API decimals stay exact when grouping forecasts, including large contract values. */
const units = (amount: string) => {
  const [whole, fraction = ""] = amount.split(".");
  return BigInt(whole) * 10000n + BigInt(fraction.padEnd(4, "0").slice(0, 4));
};
export function crmMoney(amounts: string[], currency: string): string {
  const cents = (amounts.reduce((total, amount) => total + units(amount), 0n) + 50n) / 100n;
  const separator = new Intl.NumberFormat().formatToParts(1.1).find(p => p.type === "decimal")?.value || ".";
  return `${currency} ${(cents / 100n).toLocaleString()}${separator}${String(cents % 100n).padStart(2, "0")}`;
}
