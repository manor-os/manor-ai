/**
 * A Workspace App block's records shown the way each post will look where it goes: one presentation with a
 * neutral frame per platform (its layout and proportions, never its branding), the words counted against the
 * platform's limits, the hashtags as one copyable group, its receipt and counts once it is out, the replies a
 * Workflow collected, and a post's platform versions side by side.
 */
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

import type { WorkspaceAppBlockPreview, WorkspaceAppDataItem, WorkspaceAppPostPreview, WorkspaceAppPreviewFile } from "../../lib/api";
import { copyText } from "../../lib/clipboard";
import { formatDateCompact, formatDateLong, relativeTime } from "../../lib/format";
import { t } from "../../lib/i18n";
import { useToastStore } from "../../stores/toast";
import { IconBookmark, IconCheck, IconComment, IconCopy, IconExternalLink, IconHeart, IconMoreHorizontal, IconPlay, IconRefresh, IconSend, IconShare, IconThumbUp } from "../icons";
import TabSwitcher from "../ui/TabSwitcher";
import { MediaCarousel, MediaPlayer, PackCover } from "./WorkspaceAppMedia";
import { aspectRatioCss } from "./workspaceAppMediaPresentation";
import { hashtagText, pictureGridColumns, postText, previewCounts, previewFrame, previewMedia, previewRows, previewSafeArea, threadCounts } from "./workspaceAppPostFrames";
import "./WorkspaceAppPostPreview.css";

const copy = (key: string, vars?: Record<string, string | number>) => t(`component.workspace_app_view.preview_${key}`, vars);

/** A platform's name, as text: the frame never carries its logo. */
export function previewPlatformName(preview: Pick<WorkspaceAppPostPreview, "platform" | "platform_label">): string {
  return preview.platform ? copy(`platform_${preview.platform}`) : preview.platform_label || copy("platform_post");
}

function isSafeHref(href?: string): href is string {
  return Boolean(href && (/^\/(?![\/\\])/.test(href) || /^https:\/\//i.test(href)));
}

function hostname(href: string): string {
  try { return new URL(href).hostname.replace(/^www\./, ""); } catch { return href; }
}

/** A picture of the post: a Document's own picture, or a public picture address. */
function Picture({ file, label }: { file: WorkspaceAppPreviewFile; label: string }) {
  if (file.document_id) return <PackCover documentId={file.document_id} label={label} />;
  return file.image_url && isSafeHref(file.image_url) ? <img src={file.image_url} alt="" loading="lazy" referrerPolicy="no-referrer" /> : null;
}

/** The post's clip: its cover (or first frame) until the person plays it here. */
function Clip({ video, cover, label }: { video: WorkspaceAppPreviewFile; cover?: WorkspaceAppPreviewFile; label: string }) {
  const [playing, setPlaying] = useState(false);
  if (playing && video.document_id) return <MediaPlayer file={{ document_id: video.document_id }} label={label} kind="video" autoStart />;
  return <button type="button" className="workspace-app-preview__clip" aria-label={t("component.workspace_app_view.data_play_video", { label })}
    onClick={(event) => { event.stopPropagation(); setPlaying(true); }} disabled={!video.document_id}>
    {cover ? <Picture file={cover} label={label} /> : <PackCover documentId={video.document_id} label={label} video />}
    <span className="workspace-app-preview__play" aria-hidden="true"><IconPlay size={18} /></span>
  </button>;
}

/** The pictures (or clip) in the frame's own shape: one, a swiped carousel, or a grid of up to `max`. */
function Media({ media, label, aspect, layout = "carousel", max = 4 }: {
  media: ReturnType<typeof previewMedia>; label: string; aspect?: string; layout?: "carousel" | "grid" | "first"; max?: number;
}) {
  const style: CSSProperties | undefined = aspect && aspect !== "auto" ? { aspectRatio: aspectRatioCss(aspect) } : undefined;
  if (media.video) return <span className="workspace-app-preview__media" style={style}><Clip video={media.video} cover={media.cover || media.images[0]} label={label} /></span>;
  const images = media.images.length ? media.images : media.cover ? [media.cover] : [];
  if (!images.length) return null;
  if (layout === "first" || images.length === 1) {
    return <span className="workspace-app-preview__media" style={style}><Picture file={images[0]} label={label} />
      {layout === "first" && images.length > 1 && <span className="workspace-app-preview__badge">{`1/${images.length}`}</span>}</span>;
  }
  if (layout === "carousel") {
    return <MediaCarousel count={images.length} label={copy("pictures", { count: images.length })}>
      {images.map((file, index) => <span key={file.document_id || file.image_url || index} className="workspace-app-preview__media" style={style}>
        <Picture file={file} label={`${label} ${index + 1}`} /></span>)}
    </MediaCarousel>;
  }
  const shown = images.slice(0, max);
  const columns = max === 9 ? pictureGridColumns(shown.length) : Math.min(2, shown.length);
  return <span className={`workspace-app-preview__grid is-${shown.length}`} style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
    role="group" aria-label={copy("pictures", { count: images.length })}>
    {shown.map((file, index) => <span key={file.document_id || file.image_url || index} className="workspace-app-preview__grid-item">
      <Picture file={file} label={`${label} ${index + 1}`} />
      {index === shown.length - 1 && images.length > shown.length && <span className="workspace-app-preview__more">+{images.length - shown.length}</span>}
    </span>)}
  </span>;
}

function Avatar({ preview, size = 32 }: { preview: WorkspaceAppPostPreview; size?: number }) {
  const name = preview.account || copy("account");
  return <span className="workspace-app-preview__avatar" style={{ width: size, height: size }} aria-hidden="true">
    {preview.avatar ? <Picture file={preview.avatar} label={name} /> : <span>{Array.from(name)[0]?.toUpperCase()}</span>}
  </span>;
}

/** The caption as the platform shows it: a few lines, then "more"; its hashtags after it. */
function Caption({ preview, lines, lead, tags = true }: { preview: WorkspaceAppPostPreview; lines?: number; lead?: ReactNode; tags?: boolean }) {
  const [open, setOpen] = useState(false);
  const text = preview.caption || "";
  const tagText = tags ? hashtagText(preview.hashtags) : "";
  if (!text && !tagText && !lead) return null;
  const whole = `${text} ${tagText}`;
  const long = Boolean(lines) && (whole.split("\n").length > lines! || whole.length > lines! * 50);
  return <div className="workspace-app-preview__caption">
    <p className={long && !open ? "is-clamped" : undefined} style={long && !open ? { WebkitLineClamp: lines } : undefined}>
      {lead}{lead && " "}{text}{tagText && <>{text && " "}<span className="workspace-app-preview__tags">{tagText}</span></>}
    </p>
    {long && <button type="button" className="workspace-app-preview__more-text" aria-expanded={open} onClick={(event) => { event.stopPropagation(); setOpen(!open); }}>
      {open ? t("chat.show_less") : t("chat.show_more")}</button>}
  </div>;
}

function When({ at }: { at?: string }) {
  if (!at || Number.isNaN(Date.parse(at))) return null;
  // A post still to go out shows its date; one out already, how long ago.
  return <time dateTime={at} title={formatDateLong(at)}>{Date.parse(at) > Date.now() ? formatDateCompact(at) : relativeTime(at)}</time>;
}

function LinkCard({ href, title }: { href?: string; title?: string }) {
  if (!href || !isSafeHref(href)) return null;
  return <a className="workspace-app-preview__link-card" href={href} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
    <small>{hostname(href)}</small>{title && <strong>{title}</strong>}</a>;
}

function Actions({ icons }: { icons: ReactNode[] }) {
  return <span className="workspace-app-preview__actions" aria-hidden="true">{icons.map((icon, index) => <span key={index}>{icon}</span>)}</span>;
}

/** One post in its platform's frame. */
function Frame({ preview, media, label, aspect }: { preview: WorkspaceAppPostPreview; media: ReturnType<typeof previewMedia>; label: string; aspect?: string }) {
  const frame = previewFrame(preview.platform);
  const name = preview.account || copy("account");
  const header = <header className="workspace-app-preview__header"><Avatar preview={preview} /><span><strong>{name}</strong>
    <small>{preview.platform === "x" && preview.account ? `@${preview.account.replace(/\s+/g, "").toLowerCase()}${preview.published_at ? " · " : ""}` : ""}<When at={preview.published_at} /></small></span>
    <IconMoreHorizontal size={16} aria-hidden="true" /></header>;
  const frameAspect = aspect || frame.aspect;
  if (frame.shape === "vertical") {
    const safe = previewSafeArea(preview.platform);
    return <div className="workspace-app-preview__screen" style={safe ? { "--safe-top": `${safe.top * 100}%`, "--safe-bottom": `${safe.bottom * 100}%`, "--safe-right": `${safe.right * 100}%` } as CSSProperties : undefined}>
      <Media media={media} label={label} aspect="9:16" layout="first" />
      {preview.platform === "instagram_story" && <span className="workspace-app-preview__story-bar" aria-hidden="true"><i /></span>}
      <span className="workspace-app-preview__safe" aria-hidden="true" title={copy("safe_area")} />
      {preview.platform !== "instagram_story" && <Actions icons={[<IconHeart key="like" size={20} />, <IconComment key="comment" size={20} />, <IconShare key="share" size={20} />]} />}
      <div className="workspace-app-preview__overlay">
        <strong>{preview.platform === "youtube_shorts" && preview.title ? preview.title : name}</strong>
        <Caption preview={preview} lines={frame.lines} />
      </div>
    </div>;
  }
  if (frame.shape === "note") {
    return <div className="workspace-app-preview__note">
      <Media media={media} label={label} aspect={frameAspect} layout="carousel" />
      {preview.title && <h5>{preview.title}</h5>}
      <Caption preview={preview} />
      <footer><Avatar preview={preview} size={20} /><span>{name}</span><When at={preview.published_at} /><IconHeart size={14} aria-hidden="true" /></footer>
    </div>;
  }
  if (frame.shape === "video") {
    return <div className="workspace-app-preview__video-card">
      <Media media={media} label={label} aspect="16:9" layout="first" />
      <div className="workspace-app-preview__video-meta"><Avatar preview={preview} size={28} /><span>
        <strong>{preview.title || label}</strong><small>{name}{preview.published_at ? " · " : ""}<When at={preview.published_at} /></small></span></div>
      <Caption preview={preview} lines={frame.lines} />
    </div>;
  }
  if (frame.shape === "article") {
    return <article className="workspace-app-preview__article">
      {preview.title && <h5>{preview.title}</h5>}
      <small>{name}{preview.published_at ? " · " : ""}<When at={preview.published_at} /></small>
      <Media media={media} label={label} aspect="2.35:1" layout="first" />
      {preview.summary && <p className="workspace-app-preview__summary">{preview.summary}</p>}
      <Caption preview={preview} tags={false} />
    </article>;
  }
  if (frame.shape === "moments") {
    return <div className="workspace-app-preview__moments"><Avatar preview={preview} size={36} /><div>
      <strong>{name}</strong><Caption preview={preview} lines={frame.lines} />
      <Media media={media} label={label} layout="grid" max={9} />
      <LinkCard href={preview.link} title={preview.title} />
      <footer><When at={preview.published_at} /><IconMoreHorizontal size={16} aria-hidden="true" /></footer>
    </div></div>;
  }
  if (frame.shape === "pin") {
    return <div className="workspace-app-preview__pin">
      <Media media={media} label={label} aspect={aspect || "auto"} layout="first" />
      {preview.title && <h5>{preview.title}</h5>}
      <Caption preview={preview} lines={frame.lines} tags={false} />
      <footer><Avatar preview={preview} size={20} /><span>{name}</span>{preview.link && isSafeHref(preview.link) && <small>{hostname(preview.link)}</small>}</footer>
    </div>;
  }
  // A feed post: Instagram (picture first), X, LinkedIn, Facebook (words first) and a plain post.
  const pictureFirst = preview.platform === "instagram";
  const icons = preview.platform === "x" ? [<IconComment key="c" size={16} />, <IconRefresh key="r" size={16} />, <IconHeart key="h" size={16} />, <IconBookmark key="b" size={16} />]
    : preview.platform === "linkedin" || preview.platform === "facebook" ? [<IconThumbUp key="l" size={16} />, <IconComment key="c" size={16} />, <IconShare key="s" size={16} />, <IconSend key="d" size={16} />]
    : [<IconHeart key="h" size={18} />, <IconComment key="c" size={18} />, <IconSend key="s" size={18} />, <IconBookmark key="b" size={18} />];
  return <div className={`workspace-app-preview__feed is-${preview.platform || "post"}`}>
    {header}
    {preview.title && !pictureFirst && <h5>{preview.title}</h5>}
    {!pictureFirst && <Caption preview={preview} lines={frame.lines} />}
    <Media media={media} label={label} aspect={frameAspect} layout={pictureFirst ? "carousel" : "grid"} />
    {!pictureFirst && <LinkCard href={preview.link} title={media.images.length ? undefined : preview.title} />}
    <Actions icons={icons} />
    {pictureFirst && <Caption preview={preview} lines={frame.lines} lead={<strong>{name}</strong>} />}
    {preview.platform === "x" && <Thread preview={preview} />}
  </div>;
}

/** The posts after the first, each with its own count. */
function Thread({ preview }: { preview: WorkspaceAppPostPreview }) {
  const posts = threadCounts(preview);
  if (!posts.length) return null;
  return <ol className="workspace-app-preview__thread" aria-label={copy("thread", { count: posts.length })}>{posts.map((post, index) => <li key={index}>
    <Avatar preview={preview} size={24} /><span><p>{post.text}</p>
      <small className={post.over ? "is-over" : undefined}>{post.count} / 280</small></span>
  </li>)}</ol>;
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return undefined;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return <button type="button" className="workspace-app-preview__copy" aria-label={t("component.workspace_app_view.data_copy", { label })} title={label}
    onClick={async (event) => {
      event.stopPropagation();
      const toast = useToastStore.getState();
      if (await copyText(text)) { setCopied(true); toast.success(t("component.workspace_app_view.data_copied", { label })); }
      else toast.error(t("component.workspace_app_view.data_copy_failed"));
    }}>{copied ? <IconCheck size={13} aria-hidden="true" /> : <IconCopy size={13} aria-hidden="true" />}<span>{label}</span></button>;
}

function Comments({ comments }: { comments: NonNullable<WorkspaceAppPostPreview["comments"]> }) {
  return <details className="workspace-app-preview__comments">
    <summary>{copy(comments.length === 1 ? "comments_one" : "comments", { count: comments.length })}</summary>
    <ol>{comments.map((comment, index) => <li key={index}>
      <strong>{comment.author || copy("someone")}</strong> <span>{comment.text}</span>
      <small>{comment.at && <When at={comment.at} />}{typeof comment.likes === "number" && <span><IconHeart size={11} aria-hidden="true" /> {comment.likes.toLocaleString()}</span>}</small>
    </li>)}</ol>
  </details>;
}

/** One record: its post in the frame, then what the person does with it: its counts, the copyable words, its receipt. */
function PostCard({ item, config, renderFooter }: { item: WorkspaceAppDataItem; config: WorkspaceAppBlockPreview; renderFooter?: (item: WorkspaceAppDataItem) => ReactNode }) {
  // A receipt says when the post went out; the frame shows that time.
  const declared = item.preview || { platform: config.platform || null };
  const preview = !declared.published_at && item.published_at ? { ...declared, published_at: item.published_at } : declared;
  const label = item.title || item.label || previewPlatformName(preview);
  const media = previewMedia(preview, item.media || []);
  const counts = previewCounts(preview);
  const words = postText(preview);
  const tags = hashtagText(preview.hashtags);
  // A receipt (or a published record) links to the real post and says when it went out.
  const published = item.href && isSafeHref(item.href) ? item.href : null;
  const went = item.published_at || null;
  return <article className="workspace-app-preview__card" aria-label={`${previewPlatformName(preview)} · ${label}`}>
    <div className="workspace-app-preview__frame" data-platform={preview.platform || "post"}>
      <Frame preview={preview} media={media} label={label} aspect={config.aspect} />
    </div>
    <div className="workspace-app-preview__meta">
      <span className="workspace-app-preview__platform">{previewPlatformName(preview)}</span>
      {counts.map((count) => <span key={count.part} className={`workspace-app-preview__count${count.over ? " is-over" : ""}`}
        title={copy(`count_${count.part}`, { count: count.count, limit: count.limit })}>
        {copy(`count_${count.part}`, { count: count.count.toLocaleString(), limit: count.limit.toLocaleString() })}</span>)}
      {!published && preview.published_at && !Number.isNaN(Date.parse(preview.published_at)) && <span>
        {copy(Date.parse(preview.published_at) > Date.now() ? "goes_out" : "went_out", { date: formatDateLong(preview.published_at) })}</span>}
    </div>
    {(words || tags) && <div className="workspace-app-preview__copies">
      {words && <CopyButton text={words} label={copy("copy_post")} />}
      {tags && <CopyButton text={tags} label={copy(preview.hashtags!.length === 1 ? "copy_hashtags_one" : "copy_hashtags", { count: preview.hashtags!.length })} />}
    </div>}
    {published && <div className="workspace-app-preview__receipt">
      <IconCheck size={12} aria-hidden="true" />
      <span>{went ? copy("published_on", { platform: previewPlatformName(preview), date: formatDateLong(went) }) : copy("published", { platform: previewPlatformName(preview) })}</span>
      <a href={published} target="_blank" rel="noreferrer">{copy("open_post")}<IconExternalLink size={12} aria-hidden="true" /></a>
    </div>}
    {renderFooter?.(item)}
    {preview.comments?.length ? <Comments comments={preview.comments} /> : null}
  </article>;
}

/** A post's versions in one row; one at a time, by platform, when the row is too narrow. */
function PostRow({ items, config, renderFooter }: { items: WorkspaceAppDataItem[]; config: WorkspaceAppBlockPreview; renderFooter?: (item: WorkspaceAppDataItem) => ReactNode }) {
  const row = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(false);
  const [shown, setShown] = useState(items[0]?.id || "");
  useEffect(() => {
    if (!row.current) return undefined;
    const observer = new ResizeObserver(([entry]) => setNarrow(entry.contentRect.width < 640));
    observer.observe(row.current);
    return () => observer.disconnect();
  }, []);
  const current = items.find((item) => item.id === shown) || items[0];
  return <div ref={row} className="workspace-app-preview__row-wrap">
    {narrow && items.length > 1 && <TabSwitcher size="sm" ariaLabel={copy("versions")} value={current?.id || ""} onChange={setShown}
      tabs={items.map((item) => ({ key: item.id, label: previewPlatformName(item.preview || { platform: config.platform || null }) }))} />}
    <div className={`workspace-app-preview__row${narrow ? " is-narrow" : ""}`}>
      {(narrow ? (current ? [current] : []) : items).map((item) => <PostCard key={item.id} item={item} config={config} renderFooter={renderFooter} />)}
    </div>
  </div>;
}

export default function PostPreviewItems({ items, config, renderFooter }: {
  items: WorkspaceAppDataItem[]; config: WorkspaceAppBlockPreview;
  /** What a card offers beside the post: its status, its buttons and its counts. */
  renderFooter?: (item: WorkspaceAppDataItem) => ReactNode;
}) {
  if (!items.length) return null;
  if (config.layout === "side_by_side") {
    const rows = previewRows(items);
    return <div className="workspace-app-preview__rows">{rows.map((group) => {
      const title = group.key && rows.length > 1 ? group.items.find((item) => item.preview?.title)?.preview?.title || group.items[0].title || group.items[0].label : null;
      return <section key={group.key || "posts"} className="workspace-app-preview__group" aria-label={title || copy("versions")}>
        {title && <h4>{title}</h4>}
        <PostRow items={group.items} config={config} renderFooter={renderFooter} />
      </section>;
    })}</div>;
  }
  return <div className="workspace-app-preview__feedlist">{items.map((item) => <PostCard key={item.id} item={item} config={config} renderFooter={renderFooter} />)}</div>;
}
