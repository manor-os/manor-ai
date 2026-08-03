import { formatPriceUsd, isBlueprintOnSale } from "../../lib/format";
import { t } from "../../lib/i18n";

interface BlueprintPriceProps {
  priceCents?: number | null;
  listPriceCents?: number | null;
  inverse?: boolean;
  size?: "sm" | "md";
}

export default function BlueprintPrice({
  priceCents,
  listPriceCents,
  inverse = false,
  size = "md",
}: BlueprintPriceProps) {
  const currentPrice = priceCents ?? 0;
  const onSale = isBlueprintOnSale({
    price_cents: currentPrice,
    list_price_cents: listPriceCents,
  });
  const currentLabel = currentPrice > 0
    ? formatPriceUsd(currentPrice)
    : t("page.blueprints.free");
  const muted = inverse ? "rgba(255, 255, 255, 0.62)" : "var(--text-muted)";
  const strong = inverse ? "#fff" : "var(--text-strong)";

  return (
    <span
      aria-label={onSale
        ? `${formatPriceUsd(listPriceCents)} ${currentLabel}`
        : currentLabel}
      style={{
        display: "inline-flex",
        alignItems: "baseline",
        gap: size === "sm" ? 5 : 7,
        minWidth: 0,
        whiteSpace: "nowrap",
        fontSize: size === "sm" ? 10 : 15,
        lineHeight: 1.2,
      }}
    >
      {onSale && (
        <span
          className="mono"
          style={{
            color: muted,
            fontSize: size === "sm" ? 9 : 12,
            textDecoration: "line-through",
            textDecorationThickness: "1px",
          }}
        >
          {formatPriceUsd(listPriceCents)}
        </span>
      )}
      <span className={currentPrice > 0 ? "mono" : undefined} style={{ color: strong, fontWeight: 800 }}>
        {currentLabel}
      </span>
    </span>
  );
}
