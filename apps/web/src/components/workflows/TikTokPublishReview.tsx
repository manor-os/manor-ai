import { useCallback, useEffect, useRef, useState } from "react";
import { api, isTikTokApprovalPreviewUrl } from "../../lib/api";
import { getPreferredTimeZone } from "../../lib/format";
import Button from "../ui/Button";
import Checkbox from "../ui/Checkbox";
import Select from "../ui/Select";
import Textarea from "../ui/Textarea";
import MediaPreview from "./MediaPreview";

interface TikTokReview {
  kind: "tiktok_publish";
  version: 1 | 2;
  review_id: string;
  creator_info: {
    creator_nickname: string;
    privacy_level_options: string[];
    comment_disabled: boolean;
    duet_disabled: boolean;
    stitch_disabled: boolean;
    max_video_post_duration_sec: number;
  };
  media: {
    type: "video" | "photo";
    url?: string;
    name?: string;
    duration_sec?: number;
    items?: Array<{ url: string; name: string; type: "image"; size_bytes?: number }>;
  };
  initial_caption: string;
}

interface PublicationWindow {
  deadline_at: string;
  server_now: string;
  expired: boolean;
  run_url: string;
}

export interface TikTokPublishConsent {
  kind: "tiktok_publish";
  version: 1 | 2;
  review_id: string;
  caption: string;
  privacy_level: string;
  allow_comment: boolean;
  allow_duet: boolean;
  allow_stitch: boolean;
  commercial_content: boolean;
  brand_organic_toggle: boolean;
  brand_content_toggle: boolean;
  music_usage_confirmed: boolean;
  branded_content_policy_confirmed: boolean;
  consent_confirmed: boolean;
  photo_cover_index?: number;
}

const PRIVACY_LABELS: Record<string, string> = {
  PUBLIC_TO_EVERYONE: "Everyone",
  MUTUAL_FOLLOW_FRIENDS: "Friends",
  FOLLOWER_OF_CREATOR: "Followers",
  SELF_ONLY: "Only me",
};

export function isTikTokPublishApproval(hitl: { tool?: string; review?: unknown }): boolean {
  const review = hitl.review as Partial<TikTokReview> | null;
  return review?.kind === "tiktok_publish"
    || /^(?:mcp__)?tiktok(?:__|[./])post_(?:video|photo)$/.test(hitl.tool || "");
}

function parseReview(value: unknown): TikTokReview {
  const review = value as Partial<TikTokReview> | null;
  const creator = review?.creator_info;
  const photoItems = review?.media?.items;
  const validMedia = review?.media?.type === "video"
    ? typeof review.media.url === "string" && typeof review.media.duration_sec === "number"
    : review?.media?.type === "photo" && review.version === 2 && Array.isArray(photoItems)
      && photoItems.length >= 1 && photoItems.length <= 35
      && photoItems.every((item) => item?.type === "image" && typeof item.url === "string" && typeof item.name === "string");
  if (review?.kind !== "tiktok_publish" || (review.version !== 1 && review.version !== 2)
    || typeof review.review_id !== "string" || !review.review_id
    || !creator || typeof creator.creator_nickname !== "string" || !creator.creator_nickname.trim()
    || !Array.isArray(creator.privacy_level_options) || !creator.privacy_level_options.length
    || creator.privacy_level_options.some((level) => !Object.hasOwn(PRIVACY_LABELS, level))
    || [creator.comment_disabled, creator.duet_disabled, creator.stitch_disabled].some((flag) => typeof flag !== "boolean")
    || !Number.isFinite(creator.max_video_post_duration_sec) || creator.max_video_post_duration_sec <= 0
    || !review.media || !validMedia
    || typeof review.initial_caption !== "string") {
    throw new Error("TikTok publishing details are unavailable. Refresh the review before posting.");
  }
  return review as TikTokReview;
}

export default function TikTokPublishReview({
  hitlId, disabled, onApprove, onReject,
}: {
  hitlId: string;
  disabled?: boolean;
  onApprove: (consent: TikTokPublishConsent) => void | Promise<void>;
  onReject: () => void | Promise<void>;
}) {
  const [review, setReview] = useState<TikTokReview | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [publicationWindow, setPublicationWindow] = useState<PublicationWindow | null>(null);
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [expired, setExpired] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const requestedAt = performance.now();
    setReview(null);
    setError("");
    setPublicationWindow(null);
    setExpiresAt(null);
    setExpired(false);
    api.chat.refreshTikTokPublishReview(hitlId, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        const window = (value as { publication_window?: PublicationWindow } | null)?.publication_window;
        if (window) {
          // Use server time; an incorrect device clock must not revive a review.
          const remaining = Date.parse(window.deadline_at) - Date.parse(window.server_now);
          if (!Number.isFinite(remaining)) throw new Error("The publishing deadline is unavailable. Reopen this review.");
          const end = requestedAt + Math.max(0, remaining);
          setPublicationWindow(window);
          setExpiresAt(end);
          setExpired(window.expired || performance.now() >= end);
          if (window.expired) return;
        }
        setReview(parseReview(value));
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to refresh TikTok publishing details.");
      });
    return () => controller.abort();
  }, [hitlId, attempt]);

  useEffect(() => {
    if (expiresAt === null) return;
    const check = () => setExpired(performance.now() >= expiresAt);
    const timer = window.setInterval(check, 1000);
    window.addEventListener("focus", check);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", check); };
  }, [expiresAt]);

  const deadline = publicationWindow && new Date(publicationWindow.deadline_at).toLocaleString(undefined, { timeZone: getPreferredTimeZone(), timeZoneName: "short" });
  if (expired && publicationWindow) return (
    <div className="flex min-w-0 flex-col gap-4" role="status">
      <p className="font-medium">Publishing window ended</p>
      <p>The deadline was {deadline}. This review can no longer publish the post.</p>
      <p>Return to the workflow to arrange a new publishing window and review. Retrying this approval cannot extend it.</p>
      <div className="flex flex-wrap gap-2">
        <a className="underline" href={publicationWindow.run_url}>Open workflow run</a>
        <Button variant="outline" disabled={disabled} onClick={() => void Promise.resolve().then(onReject).catch((reason) => setError(reason instanceof Error ? reason.message : "Unable to cancel publishing."))}>Cancel publishing</Button>
      </div>
      {error && <p role="alert">{error}</p>}
    </div>
  );

  if (!review) return (
    <div className="flex min-w-0 flex-col gap-4">
      <p role={error ? "alert" : "status"}>{error || "Loading the latest TikTok account and video details…"}</p>
      <div className="flex flex-wrap gap-2">
        {error && <Button disabled={disabled} onClick={() => setAttempt((value) => value + 1)}>Retry</Button>}
        <Button variant="outline" disabled={disabled} onClick={() => void Promise.resolve().then(onReject).catch((reason) => setError(reason instanceof Error ? reason.message : "Unable to cancel publishing."))}>Cancel publishing</Button>
      </div>
    </div>
  );
  return <>
    {deadline && <p className="mb-4 text-sm" role="status">Publishing deadline: {deadline}. Publication must start before this time.</p>}
    <TikTokPublishForm key={review.review_id} review={review} disabled={disabled} onApprove={(consent) => {
      if (expiresAt !== null && performance.now() >= expiresAt) { setExpired(true); return; }
      return onApprove(consent);
    }} onReject={onReject} />
  </>;
}

function TikTokPublishForm({ review, disabled, onApprove, onReject }: {
  review: TikTokReview;
  disabled?: boolean;
  onApprove: (consent: TikTokPublishConsent) => void | Promise<void>;
  onReject: () => void | Promise<void>;
}) {
  const creator = review.creator_info;
  const [draft, setDraft] = useState<TikTokPublishConsent>({
    kind: "tiktok_publish", version: review.version, review_id: review.review_id,
    caption: review.initial_caption, privacy_level: "",
    allow_comment: false, allow_duet: false, allow_stitch: false,
    commercial_content: false, brand_organic_toggle: false, brand_content_toggle: false,
    music_usage_confirmed: false, branded_content_policy_confirmed: false, consent_confirmed: false,
  });
  const [preview, setPreview] = useState({ duration: 0, failed: false, loadedPhotoIndexes: new Set<number>() });
  const submitted = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [submissionError, setSubmissionError] = useState("");
  const locked = Boolean(disabled || submitting);
  const update = (patch: Partial<TikTokPublishConsent>) => {
    setDraft((previous) => ({ ...previous, ...patch, consent_confirmed: false }));
  };
  const previewReady = useCallback((duration: number) => setPreview({ duration, failed: false, loadedPhotoIndexes: new Set() }), []);
  const photoPreviewReady = useCallback((index: number) => {
    setPreview((previous) => {
      const loadedPhotoIndexes = new Set(previous.loadedPhotoIndexes);
      loadedPhotoIndexes.add(index);
      return { ...previous, failed: false, loadedPhotoIndexes };
    });
  }, []);
  const previewError = useCallback(() => {
    setPreview({ duration: 0, failed: true, loadedPhotoIndexes: new Set() });
    setDraft((previous) => ({ ...previous, consent_confirmed: false }));
  }, []);
  const isVideo = review.media.type === "video";
  const photoItems = review.media.type === "photo" ? review.media.items || [] : [];
  const previewUrlAllowed = isVideo && typeof review.media.url === "string" && (
    /^https:\/\//.test(review.media.url)
    || isTikTokApprovalPreviewUrl(review.media.url)
    || /^\/api\/v1\/fs\/(?!public\/)/.test(review.media.url)
  );
  const photosPreviewable = !isVideo && photoItems.length >= 1 && photoItems.length <= 35
    && photoItems.every((item) => (
      /^https:\/\//.test(item.url)
      || isTikTokApprovalPreviewUrl(item.url)
      || /^\/api\/v1\/fs\/(?!public\/)/.test(item.url)
    ));
  const photosReady = photoItems.every((_, index) => preview.loadedPhotoIndexes.has(index));
  const maxDuration = creator.max_video_post_duration_sec;
  const videoDuration = isVideo ? review.media.duration_sec ?? 0 : 0;
  const durationValid = [videoDuration, preview.duration]
    .every((duration) => Number.isFinite(duration) && duration > 0 && duration <= maxDuration);
  type Interaction = "comment" | "duet" | "stitch";
  const interactions: Array<[Interaction, string]> = isVideo
    ? [["comment", "Comment"], ["duet", "Duet"], ["stitch", "Stitch"]]
    : [["comment", "Comment"]];
  const interactionDisabled = (kind: Interaction) => {
    if (kind === "comment") return creator.comment_disabled;
    if (kind === "duet") return creator.duet_disabled;
    return creator.stitch_disabled;
  };
  const interactionAllowed = (kind: Interaction) => {
    if (kind === "comment") return draft.allow_comment;
    if (kind === "duet") return draft.allow_duet;
    return draft.allow_stitch;
  };
  const updateInteraction = (kind: Interaction, value: boolean) => {
    if (kind === "comment") update({ allow_comment: value });
    else if (kind === "duet") update({ allow_duet: value });
    else update({ allow_stitch: value });
  };
  const branded = draft.commercial_content && draft.brand_content_toggle;
  const disclosureValid = !draft.commercial_content || draft.brand_organic_toggle || draft.brand_content_toggle;
  const privacyValid = creator.privacy_level_options.includes(draft.privacy_level) && !(branded && draft.privacy_level === "SELF_ONLY");
  const captionValid = draft.caption.length <= 2200;
  const captionError = captionValid ? undefined
    : `Caption is too long (${draft.caption.length}/2200). TikTok counts some emoji as two characters; shorten it before posting.`;
  const mediaValid = isVideo ? previewUrlAllowed && !preview.failed && durationValid : photosPreviewable && photosReady && !preview.failed;
  const coverValid = isVideo || (typeof draft.photo_cover_index === "number" && draft.photo_cover_index >= 0 && draft.photo_cover_index < photoItems.length);
  const valid = mediaValid && coverValid && privacyValid && disclosureValid && captionValid
    && draft.music_usage_confirmed && (!branded || draft.branded_content_policy_confirmed) && draft.consent_confirmed;
  let disabledReason = "";
  if (isVideo && (!previewUrlAllowed || preview.failed)) disabledReason = "The video preview could not load. Cancel and request a new review.";
  else if (!isVideo && !mediaValid) disabledReason = "Every photo must load before posting. Cancel and request a new review.";
  else if (isVideo && (!Number.isFinite(videoDuration) || videoDuration <= 0)) disabledReason = "The video duration could not be verified.";
  else if (isVideo && (videoDuration > maxDuration || preview.duration > maxDuration)) disabledReason = `This video exceeds the account’s ${maxDuration}-second limit.`;
  else if (isVideo && !durationValid) disabledReason = "Load or play the video preview before posting.";
  else if (!coverValid) disabledReason = "Choose the cover photo to use for this post.";
  else if (!captionValid) disabledReason = captionError || "Caption is invalid.";
  else if (!privacyValid) disabledReason = "Choose who can view this post.";
  else if (!disclosureValid) disabledReason = "Indicate whether this content promotes your brand, another brand, or both.";
  else if (!draft.music_usage_confirmed || (branded && !draft.branded_content_policy_confirmed)) disabledReason = "Confirm the applicable TikTok policies.";
  else if (!draft.consent_confirmed) disabledReason = `Confirm that you want to upload and post ${isVideo ? "this video" : "these photos"}.`;

  const submit = async (approve: boolean) => {
    if (locked || submitted.current || (approve && !valid)) return;
    submitted.current = true;
    setSubmitting(true);
    setSubmissionError("");
    try {
      if (approve) await onApprove(draft);
      else await onReject();
    } catch (reason) {
      submitted.current = false;
      setSubmitting(false);
      setSubmissionError(reason instanceof Error ? reason.message : "Unable to submit this review. Please try again.");
    }
  };

  return (
    <div className="flex min-w-0 flex-col gap-5 text-sm" data-tiktok-publish-review>
      <p>Posting to <strong className="break-words">{creator.creator_nickname}</strong></p>
      {isVideo && previewUrlAllowed && (
        <MediaPreview refItem={{ url: review.media.url || "", name: review.media.name, type: "video" }} maxHeight={300}
          onVideoReady={previewReady} onPreviewError={previewError} />
      )}
      {!isVideo && <div className="grid grid-cols-2 gap-2" aria-label="TikTok photo previews">
        {photoItems.map((item, index) => <button key={item.url} type="button" disabled={locked}
          aria-pressed={draft.photo_cover_index === index} aria-label={`Use ${item.name} as cover photo`}
          className={draft.photo_cover_index === index ? "rounded border-2 border-primary p-1" : "rounded border p-1"}
          onClick={() => update({ photo_cover_index: index })}><MediaPreview refItem={item} maxHeight={220}
            onPreviewReady={() => photoPreviewReady(index)} onPreviewError={previewError} /></button>)}
      </div>}
      {isVideo && <p className="text-xs text-stone-500">Maximum video duration: {maxDuration} seconds.</p>}
      <Textarea label="Caption" ariaLabel="TikTok caption" value={draft.caption} disabled={locked} error={captionError}
        onChange={(event) => update({ caption: event.target.value })} />
      <div className="flex flex-col gap-2">
        <span>Who can view this post?</span>
        <Select ariaLabel="TikTok privacy" value={draft.privacy_level} placeholder="Select privacy" disabled={locked}
          options={creator.privacy_level_options.map((level) => ({
            value: level, label: PRIVACY_LABELS[level], disabled: branded && level === "SELF_ONLY",
            description: branded && level === "SELF_ONLY" ? "Branded content visibility cannot be set to private." : undefined,
          }))} onChange={(privacy_level) => update({ privacy_level })} />
        {branded && <p className="text-xs text-stone-500">Branded content cannot be private. “Only me” is unavailable.</p>}
      </div>
      <fieldset className="flex min-w-0 flex-col gap-3">
        <legend className="mb-3 font-medium">Allow viewers to</legend>
        {interactions.map(([kind, label]) => (
          <Checkbox key={kind} label={`${label}${interactionDisabled(kind) ? " (disabled in TikTok settings)" : ""}`}
            checked={interactionAllowed(kind)} disabled={locked || interactionDisabled(kind)}
            onChange={(value) => updateInteraction(kind, value)} />
        ))}
      </fieldset>
      <div className="flex flex-col gap-3">
        <Checkbox label="Disclose commercial content" checked={draft.commercial_content} disabled={locked}
          onChange={(commercial_content) => update({ commercial_content, brand_organic_toggle: false, brand_content_toggle: false, branded_content_policy_confirmed: false })} />
        <p className="text-xs text-stone-500">Turn this on if this post promotes yourself, a brand, product, or service.</p>
        {draft.commercial_content && <>
          <Checkbox label="Your brand" checked={draft.brand_organic_toggle} disabled={locked}
            onChange={(brand_organic_toggle) => update({ brand_organic_toggle })} />
          <p className="text-xs text-stone-500">You are promoting yourself or your own business.</p>
          <Checkbox label="Branded content" checked={draft.brand_content_toggle} disabled={locked || draft.privacy_level === "SELF_ONLY"}
            onChange={(brand_content_toggle) => update({ brand_content_toggle, branded_content_policy_confirmed: false })} />
          <p className="text-xs text-stone-500">You are promoting another brand or a third party.{draft.privacy_level === "SELF_ONLY" ? " Choose a non-private audience to enable branded content." : ""}</p>
          {(draft.brand_organic_toggle || branded) && <p role="status">Your post will be labeled as “{branded ? "Paid partnership" : "Promotional content"}”.</p>}
          {!disclosureValid && <p role="status">Choose your brand, branded content, or both to continue.</p>}
        </>}
      </div>
      <div className="flex flex-col gap-3">
        <p>By posting, you agree to TikTok’s {branded && <><a className="underline" href="https://www.tiktok.com/legal/page/global/bc-policy/en" target="_blank" rel="noreferrer">Branded Content Policy</a> and </>}<a className="underline" href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank" rel="noreferrer">Music Usage Confirmation</a>.</p>
        <Checkbox label="I agree to TikTok’s Music Usage Confirmation" checked={draft.music_usage_confirmed} disabled={locked}
          onChange={(music_usage_confirmed) => update({ music_usage_confirmed })} />
        {branded && <Checkbox label="I agree to TikTok’s Branded Content Policy" checked={draft.branded_content_policy_confirmed} disabled={locked}
          onChange={(branded_content_policy_confirmed) => update({ branded_content_policy_confirmed })} />}
        <Checkbox label={`I consent to upload and post ${isVideo ? "this video" : "these photos"} to the TikTok account shown above with these settings.`}
          checked={draft.consent_confirmed} disabled={locked}
          onChange={(consent_confirmed) => setDraft((previous) => ({ ...previous, consent_confirmed }))} />
      </div>
      <p className="text-xs text-stone-500">TikTok may take a few minutes to process this post before it appears on your profile. The publishing result will appear when processing finishes.</p>
      {disabledReason && <p role="status">{disabledReason}</p>}
      {submissionError && <p role="alert">{submissionError}</p>}
      <div className="flex flex-wrap gap-2" title={disabledReason || undefined}>
        <Button variant="primary" disabled={locked || !valid} onClick={() => void submit(true)}>Post to TikTok</Button>
        <Button variant="outline" disabled={locked} onClick={() => void submit(false)}>Cancel publishing</Button>
      </div>
    </div>
  );
}
