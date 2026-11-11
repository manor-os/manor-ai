import { lazy, Suspense, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { AssistantResponseSurfaceBlock } from "../../lib/chatStream";
import { resolveIntegrationBrand } from "../../lib/brands/catalog";
import { t } from "../../lib/i18n";
import {
  chartScale,
  formatCardNumber,
  normalizeResponseCard,
  type ChartCard,
  type CommentCard,
  type CommentStatus,
  type DeliveryCard,
  type EmailCard,
  type EventCard,
  type EventCardItem,
  type MapCard,
  type PersonCard,
  type PostCard,
  type PostCardItem,
  type ProductCard,
  type SearchResultsCard,
  type StockCard,
} from "../../lib/responseCards";
import { resultFilePreviewTarget } from "../../lib/resultFiles";
import ChatMarkdown from "../ChatMarkdown";
import IntegrationLogo from "../IntegrationLogo";
import { useDocumentThumbnail } from "../ui/useDocumentThumbnail";

function CardLink({ href, className, children }: { href?: string; className?: string; children: ReactNode }) {
  if (!href) return <span className={className}>{children}</span>;
  return <a className={className} href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
}

// The map library loads only when a reply actually has a map.
const ResponseCardMap = lazy(() => import("./ResponseCardMap"));

const CheckMark = () => <svg className="response-card__mark" width="14" height="14" viewBox="0 0 16 16" fill="none"
  stroke="var(--success, #34a36b)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7" /></svg>;
// Scheduled has not happened yet, so it must not read as done.
const ClockMark = () => <svg className="response-card__mark" width="14" height="14" viewBox="0 0 16 16" fill="none"
  stroke="var(--text-muted)" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true"><circle cx="8" cy="8" r="5.5" /><path d="M8 5v3l2 1.5" /></svg>;
const AttentionMark = () => <span className="response-card__mark" aria-hidden="true"><span className="response-card__dot" /></span>;

// Long drafts stay readable in full, but do not push the rest of the reply off the screen.
function FoldedText({ text }: { text: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [open, setOpen] = useState(false);
  const [long, setLong] = useState(false);
  useLayoutEffect(() => {
    const element = ref.current;
    if (element && !open) setLong(element.scrollHeight > element.clientHeight + 1);
  }, [text, open]);
  return <>
    <p ref={ref} className={open ? "response-card__body" : "response-card__body response-card__body--folded"}>{text}</p>
    {long ? <button type="button" className="response-card__more" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      {t(open ? "component.chat_file_preview.collapse" : "component.chat_file_preview.expand")}
    </button> : null}
  </>;
}

function EmailView({ card }: { card: EmailCard }) {
  const [open, setOpen] = useState(false);
  const hasMore = Boolean(card.body && card.body !== card.snippet);
  const draft = card.status === "draft";
  const content = open || !card.snippet ? card.body : card.snippet;
  return <div className="response-card__inset">
    {draft ? <div className="response-card__fields">
      {card.to?.length ? <div><span>{t("component.response_card.email_to")}</span>{card.to.join("、")}</div> : null}
      <div><span>{t("component.response_card.email_subject")}</span>{card.subject}</div>
    </div> : <>
      <div className="response-card__head">
        <div className="response-card__who">
          <strong>{card.from_name || card.from_address}</strong>
          {card.from_name && card.from_address ? <span>{card.from_address}</span> : null}
        </div>
        {card.sent_at ? <span className="response-card__meta">{card.sent_at}</span> : null}
      </div>
      <CardLink href={card.url} className="response-card__title">{card.subject}</CardLink>
    </>}
    {draft && content ? <FoldedText text={content} /> : <p className="response-card__body response-card__body--muted">{content}</p>}
    {card.attachments?.map((file) => <CardLink key={file.name} href={file.url} className="response-card__file">{file.name}</CardLink>)}
    {hasMore && card.snippet ? <button type="button" className="response-card__more" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      {t(open ? "component.chat_file_preview.collapse" : "component.response_card.show_full")}
    </button> : null}
  </div>;
}

const deliveryStatusKey = {
  sent: "component.response_card.status_sent",
  published: "component.response_card.status_published",
  scheduled: "component.response_card.status_scheduled",
  failed: "component.response_card.status_failed",
} as const;

function DeliveryView({ card }: { card: DeliveryCard }) {
  return <ul className="response-card__rows">
    {card.items.map((item, index) => <li key={`${item.label}-${index}`}>
      <CardLink href={item.url} className="response-card__row">
        {item.status === "failed" ? <AttentionMark /> : item.status === "scheduled" ? <ClockMark /> : <CheckMark />}
        <span className="response-card__row-main">
          <span>{item.label}</span>
          <span className="response-card__meta">{item.detail || t(deliveryStatusKey[item.status])}</span>
        </span>
        {item.count !== undefined ? <span className="response-card__count">{item.count.toLocaleString()}</span> : null}
      </CardLink>
    </li>)}
  </ul>;
}

function SearchResultsView({ card }: { card: SearchResultsCard }) {
  return <>
    <ul className="response-card__rows">
      {card.results.map((result, index) => <li key={`${result.title}-${index}`}>
        <CardLink href={result.url} className="response-card__result">
          <strong>{result.title}</strong>
          {result.snippet ? <span>{result.snippet}</span> : null}
          <span className="response-card__meta">{[result.source, result.when].filter(Boolean).join(" · ")}</span>
        </CardLink>
      </li>)}
    </ul>
    {card.total && card.total > card.results.length
      ? <p className="response-card__note">{t("component.response_card.results_shown", { shown: card.results.length, total: card.total })}</p>
      : null}
  </>;
}

const postStatusKey = {
  draft: "component.response_card.status_draft",
  scheduled: "component.response_card.status_scheduled",
  published: "component.response_card.status_published",
  failed: "component.response_card.status_failed",
} as const;

function PlatformName({ platform, account }: { platform: string; account?: string }) {
  return <strong className="response-card__platform">
    {resolveIntegrationBrand(platform) ? <IntegrationLogo provider={platform} size={16} /> : null}
    <span>{[platform, account].filter(Boolean).join(" · ")}</span>
  </strong>;
}

function MediaTile({ file, className, video = false }: { file: { name: string; url?: string }; className: string; video?: boolean }) {
  const target = file.url ? resultFilePreviewTarget(file.url, window.location.origin) : null;
  const documentId = target && "documentId" in target ? target.documentId : null;
  const thumbnail = useDocumentThumbnail(documentId, file.name, { enabled: Boolean(documentId) });
  return <CardLink href={file.url} className={className}>
    {thumbnail.src ? <img src={thumbnail.src} alt={file.name} loading="lazy" /> : <span>{file.name}</span>}
    {video ? <svg className="response-card__play" width="28" height="28" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M5.5 4v8l6.5-4z" /></svg> : null}
  </CardLink>;
}

function PostMedia({ post }: { post: PostCardItem }) {
  if (post.images?.length) {
    // An image post swipes sideways, so the next image always shows at the edge.
    const shape = post.format === "portrait_image" ? "portrait_image" : "square_image";
    return <div className="response-card__gallery" tabIndex={0} role="group" aria-label={t("component.response_card.image_count", { count: post.images.length })}>
      {post.images.map((image, index) => <MediaTile key={`${image.name}-${index}`} file={image} className={`response-card__media response-card__tile--${shape}`} />)}
    </div>;
  }
  if (!post.media || post.format === "text") return null;
  return <MediaTile file={post.media} className={`response-card__media response-card__media--${post.format}`} video={post.format.endsWith("_video")} />;
}

function PostView({ card }: { card: PostCard }) {
  return <div className="response-card__posts" data-count={card.posts.length}>
    {card.posts.map((post, index) => <article key={`${post.platform}-${index}`} className="response-card__inset" aria-label={post.platform}>
      <div className="response-card__head">
        <PlatformName platform={post.platform} account={post.account} />
        <span className="response-card__meta">
          <CardLink href={post.url}>{t(postStatusKey[post.status])}</CardLink>
          {(post.images?.length || 0) > 1 ? ` · ${t("component.response_card.image_count", { count: post.images?.length ?? 0 })}` : ""}
        </span>
      </div>
      <PostMedia post={post} />
      {post.title ? <div className="response-card__title">{post.title}</div> : null}
      {post.text ? <FoldedText text={post.text} /> : null}
      {post.status_detail ? <p className="response-card__note">{post.status_detail}</p> : null}
      {post.metrics?.length ? <dl className="response-card__metrics">
        {post.metrics.map((metric) => <div key={metric.label}><dt>{metric.label}</dt><dd>{metric.value}</dd></div>)}
      </dl> : null}
    </article>)}
  </div>;
}

const commentStatusKey: Record<CommentStatus, Parameters<typeof t>[0]> = {
  draft: "component.response_card.status_draft",
  scheduled: "component.response_card.status_scheduled",
  sent: "component.response_card.status_sent",
  failed: "component.response_card.status_failed",
};

function CommentView({ card }: { card: CommentCard }) {
  return <div className="response-card__inset">
    <PlatformName platform={card.platform} account={card.account} />
    {card.post_title ? <CardLink href={card.post_url} className="response-card__post-title">{card.post_title}</CardLink> : null}
    <ul className="response-card__comments">
      {card.comments.map((comment, index) => <li key={`${comment.author}-${index}`}>
        <div className="response-card__head">
          <CardLink href={comment.url} className="response-card__author">{comment.author}</CardLink>
          <span className="response-card__meta">{[
            comment.status ? t(commentStatusKey[comment.status]) : "",
            comment.when,
            comment.likes ? t("component.response_card.like_count", { count: comment.likes.toLocaleString() }) : "",
          ].filter(Boolean).join(" · ")}</span>
        </div>
        <FoldedText text={comment.text} />
        {comment.reply ? <div className="response-card__reply">
          <div className="response-card__head">
            <span className="response-card__author">{comment.reply.author || t("component.response_card.reply")}</span>
            <span className="response-card__meta">{t(commentStatusKey[comment.reply.status])}</span>
          </div>
          <FoldedText text={comment.reply.text} />
        </div> : null}
      </li>)}
    </ul>
    {card.total && card.total > card.comments.length
      ? <p className="response-card__note">{t("component.response_card.results_shown", { total: card.total.toLocaleString(), shown: card.comments.length })}</p>
      : null}
  </div>;
}

function PersonView({ card }: { card: PersonCard }) {
  return <div className="response-card__inset response-card__person">
    <span className="response-card__avatar" aria-hidden="true">{Array.from(card.name)[0]}</span>
    <div className="response-card__who">
      <CardLink href={card.url} className="response-card__title">{card.name}</CardLink>
      {card.title || card.organization ? <span>{[card.organization, card.title].filter(Boolean).join(" · ")}</span> : null}
      {card.email || card.phone ? <span>{[card.email, card.phone].filter(Boolean).join(" · ")}</span> : null}
      {card.note ? <span>{card.note}</span> : null}
    </div>
  </div>;
}

const eventStatusKey: Partial<Record<EventCardItem["status"], Parameters<typeof t>[0]>> = {
  proposed: "component.response_card.event_proposed",
  cancelled: "component.response_card.event_cancelled",
};

function EventView({ card }: { card: EventCard }) {
  return <ul className="response-card__rows">
    {card.events.map((event, index) => {
      // A calendar day as written: read it in UTC so no viewer sees it shift.
      const day = new Date(`${event.date}T12:00:00Z`);
      const status = eventStatusKey[event.status];
      return <li key={`${event.title}-${index}`} className="response-card__event" data-status={event.status}>
        <time dateTime={event.date} className="response-card__date">
          <span>{day.toLocaleDateString(undefined, { month: "short", timeZone: "UTC" })}</span>
          <strong>{day.getUTCDate()}</strong>
          <span>{day.toLocaleDateString(undefined, { weekday: "short", timeZone: "UTC" })}</span>
        </time>
        <div className="response-card__who">
          <CardLink href={event.url} className="response-card__title">{event.title}</CardLink>
          {event.time || event.location ? <span>{[event.time, event.location].filter(Boolean).join(" · ")}</span> : null}
          {event.attendees?.length ? <span>{event.attendees.join("、")}</span> : null}
        </div>
        {status ? <span className="response-card__meta">{t(status)}</span> : null}
      </li>;
    })}
  </ul>;
}

function StockView({ card }: { card: StockCard }) {
  const signed = (value: number) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  const moves = [
    card.change !== undefined ? signed(card.change) : "",
    card.change_percent !== undefined ? `${signed(card.change_percent)}%` : "",
  ].filter(Boolean).join(" · ");
  const history = card.history;
  const low = history ? Math.min(...history) : 0;
  const range = history ? (Math.max(...history) - low) || 1 : 1;
  return <div className="response-card__inset">
    <div className="response-card__head">
      <CardLink href={card.url} className="response-card__author">{card.name} · {card.symbol}</CardLink>
      {card.as_of ? <span className="response-card__meta">{card.as_of}</span> : null}
    </div>
    <div className="response-card__quote">
      <strong>{card.currency}{card.price.toLocaleString(undefined, { maximumFractionDigits: 4 })}</strong>
      {moves ? <span className="response-card__meta">{moves}</span> : null}
    </div>
    {history ? <svg className="response-card__spark" viewBox="0 0 100 30" preserveAspectRatio="none" role="img" aria-label={`${card.symbol} ${history[0]} → ${history[history.length - 1]}`}>
      <polyline fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke" strokeLinejoin="round"
        points={history.map((value, index) => `${(index / (history.length - 1)) * 100},${28 - ((value - low) / range) * 26}`).join(" ")} />
    </svg> : null}
  </div>;
}

function ProductView({ card }: { card: ProductCard }) {
  return <ul className="response-card__rows">
    {card.products.map((product, index) => <li key={`${product.name}-${index}`} className="response-card__product">
      {product.image ? <MediaTile file={product.image} className="response-card__media response-card__product-image" /> : null}
      <div className="response-card__who">
        <CardLink href={product.url} className="response-card__title">{product.name}</CardLink>
        {product.seller || product.detail ? <span>{[product.seller, product.detail].filter(Boolean).join(" · ")}</span> : null}
        {product.price ? <strong className="response-card__price">{product.price}</strong> : null}
      </div>
    </li>)}
  </ul>;
}

function MapView({ card }: { card: MapCard }) {
  const [showMap, setShowMap] = useState(false);
  let number = 0;
  const places = card.places.map((place) => ({ ...place, number: place.lat !== undefined && place.lng !== undefined ? ++number : undefined }));
  return <div className="response-card__inset">
    {number ? <>
      <button type="button" className="response-card__more" aria-expanded={showMap}
        onClick={() => setShowMap((value) => !value)}>
        {t(showMap ? "component.response_card.hide_map" : "component.response_card.show_map")}
      </button>
      <p className="response-card__note">{t("component.response_card.map_sharing")}</p>
      {showMap ? <Suspense fallback={<div className="response-card__map" />}>
        <ResponseCardMap places={places} />
      </Suspense> : null}
    </> : null}
    <ul className="response-card__comments">
      {places.map((place, index) => {
        const href = place.url || `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent([place.name, place.address].filter(Boolean).join(" "))}`;
        return <li key={`${place.name}-${index}`} className="response-card__place">
          {number ? <span className="response-card__pin" aria-hidden="true" data-empty={place.number === undefined || undefined}>{place.number}</span> : null}
          <div className="response-card__who">
            <CardLink href={href} className="response-card__author">{place.name}</CardLink>
            {place.address || place.detail ? <span>{[place.address, place.detail].filter(Boolean).join(" · ")}</span> : null}
          </div>
        </li>;
      })}
    </ul>
    {number ? <p className="response-card__note">{t("component.response_card.map_approximate")}</p> : null}
  </div>;
}

const CHART_HEIGHT = 180;
const LEFT = 44;
const RIGHT = 16;
const TOP = 24;
const BOTTOM = 28;
const SERIES_COLORS = ["var(--chart-series-1)", "var(--chart-series-2)", "var(--chart-series-3)"];

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    setWidth(element.clientWidth || 640);
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => setWidth(element.clientWidth || 640));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

function PlotChart({ card, summary, scale }: {
  card: ChartCard;
  summary: string;
  scale: NonNullable<ReturnType<typeof chartScale>>;
}) {
  const { ref, width } = useWidth();
  const [hovered, setHovered] = useState<number | null>(null);
  const isBar = card.chart_type === "bar";
  const multi = card.series.length > 1;
  // Lines leave room on the right for their end values.
  const right = isBar ? RIGHT : RIGHT + 44;
  const plot = Math.max(120, width - LEFT - right);
  const slot = plot / Math.max(1, card.categories.length - (isBar ? 0 : 1));
  const xAt = (index: number) => LEFT + (isBar ? slot * (index + 0.5) : card.categories.length === 1 ? plot / 2 : slot * index);
  const yAt = (value: number) => TOP + (CHART_HEIGHT - TOP - BOTTOM) * (1 - (value - scale.min) / (scale.max - scale.min));
  const labelEvery = Math.ceil(card.categories.length / Math.max(2, Math.floor(plot / 56)));
  const seriesColor = (index: number) => multi ? SERIES_COLORS[index] : "var(--text-strong)";
  const baseline = yAt(Math.max(scale.min, Math.min(0, scale.max)));
  const barWidth = Math.min(24, slot * 0.6);
  const tooltipLeft = hovered === null ? 0 : Math.min(Math.max(xAt(hovered) - 60, 0), Math.max(0, width - 140));
  return <div ref={ref} className="response-card__plot" onMouseLeave={() => setHovered(null)}>
    <svg width={width} height={CHART_HEIGHT} role="img" aria-label={summary}>
      {scale.ticks.map((tick) => <g key={tick}>
        <line x1={LEFT} x2={width - right} y1={yAt(tick)} y2={yAt(tick)} className="response-card__grid" />
        <text x={LEFT - 8} y={yAt(tick) + 4} textAnchor="end" className="response-card__axis">{formatCardNumber(tick, card.unit)}</text>
      </g>)}
      {card.reference ? <g>
        <line x1={LEFT} x2={width - right} y1={yAt(card.reference.value)} y2={yAt(card.reference.value)} className="response-card__reference" />
        <text x={LEFT + 4} y={yAt(card.reference.value) - 6} className="response-card__axis">{card.reference.label}</text>
      </g> : null}
      {card.categories.map((category, index) => index % labelEvery === 0 || index === card.categories.length - 1
        ? <text key={category + index} x={xAt(index)} y={CHART_HEIGHT - 8} textAnchor="middle" className="response-card__axis">{category}</text>
        : null)}
      {isBar ? card.series[0].values.map((value, index) => {
        const x = xAt(index) - barWidth / 2;
        const y = yAt(value);
        const up = value >= 0;
        const radius = Math.min(4, Math.abs(baseline - y));
        const end = up ? y + radius : y - radius;
        return <path key={index} fill={seriesColor(0)}
          d={`M${x} ${baseline}V${end}Q${x} ${y} ${x + radius} ${y}H${x + barWidth - radius}Q${x + barWidth} ${y} ${x + barWidth} ${end}V${baseline}Z`} />;
      }) : card.series.map((series, seriesIndex) => <g key={series.name}>
        <polyline points={series.values.map((value, index) => `${xAt(index)},${yAt(value)}`).join(" ")} fill="none"
          stroke={seriesColor(seriesIndex)} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx={xAt(series.values.length - 1)} cy={yAt(series.values[series.values.length - 1])} r="4" fill={seriesColor(seriesIndex)} className="response-card__end" />
        <text x={xAt(series.values.length - 1) + 10} y={yAt(series.values[series.values.length - 1]) + 4} className="response-card__value">
          {formatCardNumber(series.values[series.values.length - 1], card.unit)}
        </text>
      </g>)}
      {hovered !== null && !isBar ? <g>
        <line x1={xAt(hovered)} x2={xAt(hovered)} y1={TOP} y2={CHART_HEIGHT - BOTTOM} className="response-card__crosshair" />
        {card.series.map((series, index) => <circle key={series.name} cx={xAt(hovered)} cy={yAt(series.values[hovered])} r="4" fill={seriesColor(index)} className="response-card__end" />)}
      </g> : null}
      {card.categories.map((category, index) => <rect key={category + index} x={xAt(index) - slot / 2} y={0} width={Math.max(slot, 24)} height={CHART_HEIGHT}
        fill="transparent" onMouseEnter={() => setHovered(index)} />)}
    </svg>
    {hovered !== null ? <div className="response-card__tooltip" style={{ left: tooltipLeft }}>
      <div>{card.categories[hovered]}</div>
      {card.series.map((series) => <strong key={series.name}>{multi ? `${series.name} ` : ""}{formatCardNumber(series.values[hovered], card.unit)}</strong>)}
    </div> : null}
  </div>;
}

function ShareChart({ card, summary }: { card: ChartCard; summary: string }) {
  const values = card.series[0].values;
  const total = values.reduce((sum, value) => sum + value, 0) || 1;
  return <>
    <div className="response-card__share" role="img" aria-label={summary}>
      {values.map((value, index) => <span key={index} style={{ flexGrow: value, background: `var(--chart-series-${index % 3 + 1})`, opacity: index < 3 ? 1 : 0.6 }} />)}
    </div>
    <ul className="response-card__legend-list">
      {card.categories.map((category, index) => <li key={category + index}>
        <span className="response-card__key"><span className="response-card__swatch" style={{ background: `var(--chart-series-${index % 3 + 1})`, opacity: index < 3 ? 1 : 0.6 }} />{category}</span>
        <span>{formatCardNumber(values[index], card.unit)} · {Math.round(values[index] / total * 100)}%</span>
      </li>)}
    </ul>
  </>;
}

function RankingChart({ card, summary }: { card: ChartCard; summary: string }) {
  const values = card.series[0].values;
  const top = Math.max(...values.map(Math.abs)) || 1;
  return <ol className="response-card__ranking" aria-label={summary}>
    {card.categories.map((category, index) => <li key={category + index}>
      <span className="response-card__ranking-name">{category}</span>
      <span className="response-card__ranking-track"><span style={{ width: `${Math.abs(values[index]) / top * 100}%` }} /></span>
      <span>{formatCardNumber(values[index], card.unit)}</span>
    </li>)}
  </ol>;
}

function ChartTable({ card }: { card: ChartCard }) {
  return <div className="response-card__table">
    <table>
      <thead><tr><th scope="col" />{card.series.map((series) => <th scope="col" key={series.name}>{series.name}</th>)}</tr></thead>
      <tbody>{card.categories.map((category, index) => <tr key={category + index}>
        <th scope="row">{category}</th>
        {card.series.map((series) => <td key={series.name}>{formatCardNumber(series.values[index], card.unit)}</td>)}
      </tr>)}</tbody>
    </table>
  </div>;
}

function ChartView({ card, title }: { card: ChartCard; title: string }) {
  const [asTable, setAsTable] = useState(false);
  const summary = useMemo(() => `${title}: ${card.series.map((series) => `${series.name} ${series.values
    .map((value, index) => `${card.categories[index]} ${formatCardNumber(value, card.unit)}`).join(", ")}`).join("; ")}`, [card, title]);
  const isPlot = card.chart_type === "line" || card.chart_type === "bar";
  const values = card.series.flatMap((series) => series.values);
  const scale = isPlot
    ? chartScale(card.reference ? [...values, card.reference.value] : values, card.chart_type === "bar")
    : null;
  if (isPlot && !scale) return <div className="response-card__inset"><ChartTable card={card} /></div>;
  const Graphic = card.chart_type === "share" ? ShareChart : RankingChart;
  return <div className="response-card__inset">
    <div className="response-card__head">
      <div className="response-card__legend">
        {card.series.length > 1 ? card.series.map((series, index) => <span key={series.name} className="response-card__key">
          <span className="response-card__swatch" style={{ background: SERIES_COLORS[index] }} />{series.name}
        </span>) : null}
      </div>
      <button type="button" className="response-card__more" aria-pressed={asTable} onClick={() => setAsTable((value) => !value)}>
        {t(asTable ? "component.response_card.view_chart" : "component.response_card.view_table")}
      </button>
    </div>
    {asTable ? <ChartTable card={card} />
      : scale ? <PlotChart card={card} summary={summary} scale={scale} />
      : <Graphic card={card} summary={summary} />}
  </div>;
}

/** A read-only card in a reply's content, drawn in the page rather than a sandboxed frame. */
export default function ResponseCard({ surface }: { surface: AssistantResponseSurfaceBlock }) {
  const render = surface.render;
  const card = render.kind === "template" ? normalizeResponseCard(render.template_id, render.props) : null;
  // A card this client cannot read still says what it holds.
  if (!card) return <ChatMarkdown content={surface.fallback_markdown} />;
  return <section className="response-card" aria-label={surface.title} data-card={card.template}>
    {card.template === "card.email" && <EmailView card={card.props} />}
    {card.template === "card.delivery" && <DeliveryView card={card.props} />}
    {card.template === "card.search_results" && <SearchResultsView card={card.props} />}
    {card.template === "card.post" && <PostView card={card.props} />}
    {card.template === "card.comment" && <CommentView card={card.props} />}
    {card.template === "card.person" && <PersonView card={card.props} />}
    {card.template === "card.event" && <EventView card={card.props} />}
    {card.template === "card.stock" && <StockView card={card.props} />}
    {card.template === "card.product" && <ProductView card={card.props} />}
    {card.template === "card.map" && <MapView card={card.props} />}
    {card.template === "card.chart" && <ChartView card={card.props} title={surface.title} />}
  </section>;
}
