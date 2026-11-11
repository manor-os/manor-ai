/**
 * Read-only cards a reply can show (packages/core/services/response_cards.py).
 * Their props come from the model and are stored with the message, so the client
 * checks them again before drawing: a card that fails shows its text fallback.
 */
export type EmailCard = {
  status: "received" | "draft";
  subject: string;
  from_name?: string;
  from_address?: string;
  to?: string[];
  sent_at?: string;
  snippet?: string;
  body?: string;
  attachments?: Array<{ name: string; url?: string }>;
  url?: string;
};
export type DeliveryCard = {
  items: Array<{ label: string; status: "sent" | "published" | "scheduled" | "failed"; count?: number; detail?: string; url?: string }>;
};
export type SearchResultsCard = {
  results: Array<{ title: string; source: string; snippet?: string; when?: string; url?: string }>;
  total?: number;
};
export type PostFormat = "vertical_video" | "horizontal_video" | "square_image" | "portrait_image" | "text";
export type PostCardItem = {
  platform: string;
  status: "draft" | "scheduled" | "published" | "failed";
  format: PostFormat;
  account?: string;
  title?: string;
  text?: string;
  media?: { name: string; url?: string };
  /** An image post: all of its images, in reading order. */
  images?: Array<{ name: string; url?: string }>;
  status_detail?: string;
  metrics?: Array<{ label: string; value: string }>;
  url?: string;
};
export type PostCard = { posts: PostCardItem[] };
export type CommentStatus = "draft" | "scheduled" | "sent" | "failed";
export type CommentCardItem = {
  author: string;
  text: string;
  when?: string;
  likes?: number;
  /** Only a comment we wrote has a status; one we received has none. */
  status?: CommentStatus;
  url?: string;
  reply?: { text: string; status: CommentStatus; author?: string };
};
export type CommentCard = {
  platform: string;
  account?: string;
  post_title?: string;
  post_url?: string;
  comments: CommentCardItem[];
  total?: number;
};
export type PersonCard = { name: string; title?: string; organization?: string; email?: string; phone?: string; note?: string; url?: string };
export type EventCardItem = { title: string; date: string; status: "proposed" | "confirmed" | "cancelled"; time?: string; location?: string; attendees?: string[]; url?: string };
export type EventCard = { events: EventCardItem[] };
export type StockCard = { name: string; symbol: string; price: number; currency?: string; change?: number; change_percent?: number; as_of?: string; history?: number[]; url?: string };
export type ProductCard = { products: Array<{ name: string; price?: string; seller?: string; detail?: string; image?: { name: string; url?: string }; url?: string }> };
export type MapPlace = { name: string; address?: string; detail?: string; lat?: number; lng?: number; url?: string };
export type MapCard = { places: MapPlace[] };
export type ChartCard = {
  chart_type: "line" | "bar" | "share" | "ranking";
  categories: string[];
  series: Array<{ name: string; values: number[] }>;
  unit?: string;
  reference?: { label: string; value: number };
};

export type ResponseCard =
  | { template: "card.email"; props: EmailCard }
  | { template: "card.delivery"; props: DeliveryCard }
  | { template: "card.search_results"; props: SearchResultsCard }
  | { template: "card.post"; props: PostCard }
  | { template: "card.comment"; props: CommentCard }
  | { template: "card.person"; props: PersonCard }
  | { template: "card.event"; props: EventCard }
  | { template: "card.stock"; props: StockCard }
  | { template: "card.product"; props: ProductCard }
  | { template: "card.map"; props: MapCard }
  | { template: "card.chart"; props: ChartCard };

export const RESPONSE_CARD_TEMPLATE_IDS = new Set([
  "card.chart", "card.comment", "card.delivery", "card.email", "card.event", "card.map", "card.person",
  "card.post", "card.product", "card.search_results", "card.stock",
]);

const LINK = /^(?:https?:\/\/[^\s<>"']+|\/viewer\/[0-9A-Za-z_-]+|\/api\/v1\/fs\/[^\s<>"']+)$/;
const POST_FORMATS = new Set<string>(["vertical_video", "horizontal_video", "square_image", "portrait_image", "text"]);
const MAX_CATEGORIES = { line: 24, bar: 24, share: 8, ranking: 12 } as const;

type Raw = Record<string, unknown>;
const record = (value: unknown): Raw | null => value && typeof value === "object" && !Array.isArray(value) ? value as Raw : null;
const text = (value: unknown, maximum: number): string => typeof value === "string" ? value.trim().slice(0, maximum) : "";
const count = (value: unknown): number | undefined => Number.isInteger(value) && Number(value) >= 0 ? Number(value) : undefined;
const list = (value: unknown, maximum: number): unknown[] | null => Array.isArray(value) && value.length <= maximum ? value : null;
const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

/** A link is kept only when it goes to the web or to one of Manor's own file addresses. */
export function responseCardLink(value: unknown): string | undefined {
  const link = text(value, 2_000);
  return LINK.test(link) ? link : undefined;
}

const named = (value: unknown) => {
  const raw = record(value);
  const name = text(raw?.name, 160);
  return name ? { name, url: responseCardLink(raw?.url) } : null;
};

function email(raw: Raw): EmailCard | null {
  const subject = text(raw.subject, 300);
  const snippet = text(raw.snippet, 600);
  const body = text(raw.body, 6_000);
  if (!subject || (!snippet && !body)) return null;
  return {
    status: raw.status === "draft" ? "draft" : "received",
    subject,
    snippet,
    body,
    from_name: text(raw.from_name, 160),
    from_address: text(raw.from_address, 254),
    to: (list(raw.to, 10) || []).map((item) => text(item, 160)).filter(Boolean),
    sent_at: text(raw.sent_at, 40),
    attachments: (list(raw.attachments, 10) || []).flatMap((item) => named(item) || []),
    url: responseCardLink(raw.url),
  };
}

function delivery(raw: Raw): DeliveryCard | null {
  const items = list(raw.items, 20);
  if (!items?.length) return null;
  const parsed = items.flatMap<DeliveryCard["items"][number]>((item) => {
    const entry = record(item);
    const label = text(entry?.label, 120);
    const status = entry?.status;
    if (!label || !(status === "sent" || status === "published" || status === "scheduled" || status === "failed")) return [];
    return [{ label, status, count: count(entry?.count), detail: text(entry?.detail, 160), url: responseCardLink(entry?.url) }];
  });
  return parsed.length ? { items: parsed } : null;
}

function searchResults(raw: Raw): SearchResultsCard | null {
  const results = list(raw.results, 10);
  if (!results?.length) return null;
  const parsed = results.flatMap((item) => {
    const entry = record(item);
    const title = text(entry?.title, 200);
    const source = text(entry?.source, 120);
    return title && source
      ? [{ title, source, snippet: text(entry?.snippet, 300), when: text(entry?.when, 40), url: responseCardLink(entry?.url) }]
      : [];
  });
  return parsed.length ? { results: parsed, total: count(raw.total) } : null;
}

function post(raw: Raw): PostCard | null {
  const posts = list(raw.posts, 6);
  if (!posts?.length) return null;
  const parsed = posts.flatMap<PostCardItem>((item) => {
    const entry = record(item);
    const platform = text(entry?.platform, 40);
    const status = entry?.status;
    if (!platform || !(status === "draft" || status === "scheduled" || status === "published" || status === "failed")) return [];
    return [{
      platform,
      status,
      format: (POST_FORMATS.has(String(entry?.format)) ? entry?.format : "text") as PostFormat,
      account: text(entry?.account, 80),
      title: text(entry?.title, 200),
      text: text(entry?.text, 2_000),
      media: named(entry?.media) || undefined,
      images: (list(entry?.images, 20) || []).flatMap((image) => named(image) || []),
      status_detail: text(entry?.status_detail, 160),
      metrics: (list(entry?.metrics, 4) || []).flatMap((metric) => {
        const raw = record(metric);
        const label = text(raw?.label, 24);
        const value = text(raw?.value, 24);
        return label && value ? [{ label, value }] : [];
      }),
      url: responseCardLink(entry?.url),
    }];
  });
  return parsed.length ? { posts: parsed } : null;
}

const COMMENT_STATUSES = new Set<unknown>(["draft", "scheduled", "sent", "failed"]);

function comment(raw: Raw): CommentCard | null {
  const platform = text(raw.platform, 40);
  const comments = list(raw.comments, 20);
  if (!platform || !comments?.length) return null;
  const parsed = comments.flatMap<CommentCardItem>((item) => {
    const entry = record(item);
    const author = text(entry?.author, 80);
    const body = text(entry?.text, 1_000);
    if (!author || !body) return [];
    const reply = record(entry?.reply);
    const replyText = text(reply?.text, 1_000);
    return [{
      author,
      text: body,
      when: text(entry?.when, 40),
      likes: count(entry?.likes),
      status: COMMENT_STATUSES.has(entry?.status) ? entry?.status as CommentStatus : undefined,
      url: responseCardLink(entry?.url),
      reply: replyText && COMMENT_STATUSES.has(reply?.status)
        ? { text: replyText, status: reply?.status as CommentStatus, author: text(reply?.author, 80) }
        : undefined,
    }];
  });
  return parsed.length ? {
    platform,
    account: text(raw.account, 80),
    post_title: text(raw.post_title, 200),
    post_url: responseCardLink(raw.post_url),
    comments: parsed,
    total: count(raw.total),
  } : null;
}

function person(raw: Raw): PersonCard | null {
  const name = text(raw.name, 120);
  return name ? {
    name,
    title: text(raw.title, 120),
    organization: text(raw.organization, 120),
    email: text(raw.email, 254),
    phone: text(raw.phone, 40),
    note: text(raw.note, 300),
    url: responseCardLink(raw.url),
  } : null;
}

function event(raw: Raw): EventCard | null {
  const events = list(raw.events, 8);
  if (!events?.length) return null;
  const parsed = events.flatMap<EventCardItem>((item) => {
    const entry = record(item);
    const title = text(entry?.title, 200);
    const date = text(entry?.date, 10);
    if (!title || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return [];
    return [{
      title,
      date,
      status: entry?.status === "proposed" || entry?.status === "cancelled" ? entry.status : "confirmed",
      time: text(entry?.time, 40),
      location: text(entry?.location, 200),
      attendees: (list(entry?.attendees, 20) || []).map((attendee) => text(attendee, 80)).filter(Boolean),
      url: responseCardLink(entry?.url),
    }];
  });
  return parsed.length ? { events: parsed } : null;
}

function stock(raw: Raw): StockCard | null {
  const name = text(raw.name, 120);
  const symbol = text(raw.symbol, 20);
  if (!name || !symbol || !isNumber(raw.price)) return null;
  const history = list(raw.history, 240);
  return {
    name,
    symbol,
    price: raw.price,
    currency: text(raw.currency, 8),
    change: isNumber(raw.change) ? raw.change : undefined,
    change_percent: isNumber(raw.change_percent) ? raw.change_percent : undefined,
    as_of: text(raw.as_of, 60),
    history: history && history.length >= 2 && history.every(isNumber) ? history as number[] : undefined,
    url: responseCardLink(raw.url),
  };
}

function product(raw: Raw): ProductCard | null {
  const products = list(raw.products, 6);
  if (!products?.length) return null;
  const parsed = products.flatMap<ProductCard["products"][number]>((item) => {
    const entry = record(item);
    const name = text(entry?.name, 200);
    return name ? [{
      name,
      price: text(entry?.price, 40),
      seller: text(entry?.seller, 120),
      detail: text(entry?.detail, 160),
      image: named(entry?.image) || undefined,
      url: responseCardLink(entry?.url),
    }] : [];
  });
  return parsed.length ? { products: parsed } : null;
}

function map(raw: Raw): MapCard | null {
  const places = list(raw.places, 12);
  if (!places?.length) return null;
  const parsed = places.flatMap<MapPlace>((item) => {
    const entry = record(item);
    const name = text(entry?.name, 160);
    if (!name) return [];
    const located = isNumber(entry?.lat) && isNumber(entry?.lng) && Math.abs(entry.lat) <= 90 && Math.abs(entry.lng) <= 180;
    return [{
      name,
      address: text(entry?.address, 240),
      detail: text(entry?.detail, 160),
      lat: located ? entry?.lat as number : undefined,
      lng: located ? entry?.lng as number : undefined,
      url: responseCardLink(entry?.url),
    }];
  });
  return parsed.length ? { places: parsed } : null;
}

/** A view that holds every located place. Positions are rough, so the fit is too. */
export function mapView(points: Array<{ lat: number; lng: number }>, width: number, height: number): { center: [number, number]; zoom: number } {
  const mercator = (lat: number) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
  const lats = points.map((point) => mercator(point.lat));
  const lngs = points.map((point) => point.lng);
  const south = Math.min(...lats);
  const north = Math.max(...lats);
  const west = Math.min(...lngs);
  const east = Math.max(...lngs);
  const center: [number, number] = [(Math.atan(Math.exp((south + north) / 2)) * 360) / Math.PI - 90, (west + east) / 2];
  if (points.length === 1 || (north === south && east === west)) return { center, zoom: 13 };
  const fit = (pixels: number, fraction: number) => fraction > 0 ? Math.log2((pixels * 0.7) / 256 / fraction) : 15;
  const zoom = Math.floor(Math.min(fit(width, (east - west) / 360), fit(height, (north - south) / (2 * Math.PI))));
  return { center, zoom: Math.min(15, Math.max(1, zoom)) };
}

function chart(raw: Raw): ChartCard | null {
  const type = raw.chart_type;
  if (type !== "line" && type !== "bar" && type !== "share" && type !== "ranking") return null;
  const categories = (list(raw.categories, MAX_CATEGORIES[type]) || []).map((item) => text(item, 40));
  const rawSeries = list(raw.series, type === "line" ? 3 : 1);
  if (!categories.length || categories.some((item) => !item) || !rawSeries?.length) return null;
  const series: ChartCard["series"] = [];
  for (const item of rawSeries) {
    const entry = record(item);
    const name = text(entry?.name, 40);
    const values = entry?.values;
    if (!name || !Array.isArray(values) || values.length !== categories.length || !values.every(isNumber)) return null;
    if (type === "share" && values.some((value) => value < 0)) return null;
    series.push({ name, values });
  }
  const reference = record(raw.reference);
  return {
    chart_type: type,
    categories,
    series,
    unit: text(raw.unit, 12),
    reference: reference && isNumber(reference.value) && text(reference.label, 40)
      ? { label: text(reference.label, 40), value: reference.value }
      : undefined,
  };
}

export function normalizeResponseCard(template: string, props: unknown): ResponseCard | null {
  const raw = record(props);
  if (!raw) return null;
  if (template === "card.email") { const value = email(raw); return value && { template, props: value }; }
  if (template === "card.delivery") { const value = delivery(raw); return value && { template, props: value }; }
  if (template === "card.search_results") { const value = searchResults(raw); return value && { template, props: value }; }
  if (template === "card.post") { const value = post(raw); return value && { template, props: value }; }
  if (template === "card.comment") { const value = comment(raw); return value && { template, props: value }; }
  if (template === "card.person") { const value = person(raw); return value && { template, props: value }; }
  if (template === "card.event") { const value = event(raw); return value && { template, props: value }; }
  if (template === "card.stock") { const value = stock(raw); return value && { template, props: value }; }
  if (template === "card.product") { const value = product(raw); return value && { template, props: value }; }
  if (template === "card.map") { const value = map(raw); return value && { template, props: value }; }
  if (template === "card.chart") { const value = chart(raw); return value && { template, props: value }; }
  return null;
}

/** A number as the person reads it: currency signs lead, every other unit follows. */
export function formatCardNumber(value: number, unit = ""): string {
  const number = value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (!unit) return number;
  return /^[¥$€£]/.test(unit) ? `${unit}${number}` : `${number}${unit === "%" ? "" : " "}${unit}`;
}

/** A few round gridline values that hold the data; bars always start from zero. */
export function chartScale(values: number[], fromZero: boolean): { min: number; max: number; ticks: number[] } | null {
  let low = Math.min(...values);
  let high = Math.max(...values);
  if (fromZero) { low = Math.min(0, low); high = Math.max(0, high); }
  if (low === high) { low -= 1; high += 1; }
  const rough = (high - low) / 3;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 5, 10].map((factor) => factor * power).find((candidate) => candidate >= rough) ?? 10 * power;
  const min = Math.floor(low / step) * step;
  const max = Math.ceil(high / step) * step;
  const span = max - min;
  if (!Number.isFinite(span) || span <= 0 || !Number.isFinite(step) || step <= 0) return null;
  // Finite inputs can still overflow when padding the axis or adding a tick.
  const count = Math.min(8, Math.round(span / step) + 1);
  const ticks = Array.from({ length: count }, (_, index) => Number((min + index * step).toPrecision(12)));
  return { min, max, ticks };
}
