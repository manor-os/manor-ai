import { useCallback, useEffect, useRef, useState } from "react";
import { api, isTikTokApprovalPreviewUrl } from "../../lib/api";
import Button from "../ui/Button";
import Checkbox from "../ui/Checkbox";
import Select from "../ui/Select";
import Textarea from "../ui/Textarea";
import MediaPreview from "./MediaPreview";

interface TikTokReview {
  kind: "tiktok_publish";
  version: 1;
  review_id: string;
  creator_info: {
    creator_nickname: string;
    privacy_level_options: string[];
    comment_disabled: boolean;
    duet_disabled: boolean;
    stitch_disabled: boolean;
    max_video_post_duration_sec: number;
  };
  media: { url: string; name: string; type: string; duration_sec: number };
  initial_caption: string;
}

export interface TikTokPublishConsent {
  kind: "tiktok_publish";
  version: 1;
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
  if (review?.media?.type === "photo") {
    throw new Error("Photo publishing is not supported in this review. Choose a video to continue.");
  }
  if (review?.kind !== "tiktok_publish" || review.version !== 1
    || typeof review.review_id !== "string" || !review.review_id
    || !creator || typeof creator.creator_nickname !== "string" || !creator.creator_nickname.trim()
    || !Array.isArray(creator.privacy_level_options) || !creator.privacy_level_options.length
    || creator.privacy_level_options.some((level) => !Object.hasOwn(PRIVACY_LABELS, level))
    || [creator.comment_disabled, creator.duet_disabled, creator.stitch_disabled].some((flag) => typeof flag !== "boolean")
    || !Number.isFinite(creator.max_video_post_duration_sec) || creator.max_video_post_duration_sec <= 0
    || !review.media || typeof review.media.url !== "string"
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
  onApprove: (consent: TikTokPublishConsent) => void;
  onReject: () => void;
}) {
  const [review, setReview] = useState<TikTokReview | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setReview(null);
    setError("");
    api.chat.refreshTikTokPublishReview(hitlId, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setReview(parseReview(value)); })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to refresh TikTok publishing details.");
      });
    return () => controller.abort();
  }, [hitlId, attempt]);

  if (!review) return (
    <div className="flex min-w-0 flex-col gap-4">
      <p role={error ? "alert" : "status"}>{error || "Loading the latest TikTok account and video details…"}</p>
      <div className="flex flex-wrap gap-2">
        {error && <Button disabled={disabled} onClick={() => setAttempt((value) => value + 1)}>Retry</Button>}
        <Button variant="outline" disabled={disabled} onClick={onReject}>Cancel publishing</Button>
      </div>
    </div>
  );
  return <TikTokPublishForm key={review.review_id} review={review} disabled={disabled} onApprove={onApprove} onReject={onReject} />;
}

function TikTokPublishForm({ review, disabled, onApprove, onReject }: {
  review: TikTokReview;
  disabled?: boolean;
  onApprove: (consent: TikTokPublishConsent) => void;
  onReject: () => void;
}) {
  const creator = review.creator_info;
  const [draft, setDraft] = useState<TikTokPublishConsent>({
    kind: "tiktok_publish", version: 1, review_id: review.review_id,
    caption: review.initial_caption, privacy_level: "",
    allow_comment: false, allow_duet: false, allow_stitch: false,
    commercial_content: false, brand_organic_toggle: false, brand_content_toggle: false,
    music_usage_confirmed: false, branded_content_policy_confirmed: false, consent_confirmed: false,
  });
  const [preview, setPreview] = useState({ duration: 0, failed: false });
  const submitted = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const locked = Boolean(disabled || submitting);
  const update = (patch: Partial<TikTokPublishConsent>) => {
    setDraft((previous) => ({ ...previous, ...patch, consent_confirmed: false }));
  };
  const previewReady = useCallback((duration: number) => setPreview({ duration, failed: false }), []);
  const previewError = useCallback(() => {
    setPreview({ duration: 0, failed: true });
    setDraft((previous) => ({ ...previous, consent_confirmed: false }));
  }, []);
  const isVideo = review.media.type === "video";
  const previewUrlAllowed = /^https:\/\//.test(review.media.url)
    || isTikTokApprovalPreviewUrl(review.media.url)
    || /^\/api\/v1\/fs\/(?!public\/)/.test(review.media.url);
  const maxDuration = creator.max_video_post_duration_sec;
  const durationValid = [review.media.duration_sec, preview.duration]
    .every((duration) => Number.isFinite(duration) && duration > 0 && duration <= maxDuration);
  const branded = draft.commercial_content && draft.brand_content_toggle;
  const disclosureValid = !draft.commercial_content || draft.brand_organic_toggle || draft.brand_content_toggle;
  const privacyValid = creator.privacy_level_options.includes(draft.privacy_level) && !(branded && draft.privacy_level === "SELF_ONLY");
  const captionValid = draft.caption.length <= 2200;
  const captionError = captionValid ? undefined
    : `Caption is too long (${draft.caption.length}/2200). TikTok counts some emoji as two characters; shorten it before posting.`;
  const valid = isVideo && previewUrlAllowed && !preview.failed && durationValid && privacyValid && disclosureValid && captionValid
    && draft.music_usage_confirmed && (!branded || draft.branded_content_policy_confirmed) && draft.consent_confirmed;
  const disabledReason = !isVideo ? "Photo publishing is not supported in this review."
    : !previewUrlAllowed || preview.failed ? "The video preview could not load. Cancel and request a new review."
    : !Number.isFinite(review.media.duration_sec) || review.media.duration_sec <= 0 ? "The video duration could not be verified."
    : review.media.duration_sec > maxDuration || preview.duration > maxDuration ? `This video exceeds the account’s ${maxDuration}-second limit.`
    : !durationValid ? "Load or play the video preview before posting."
    : !captionValid ? captionError
    : !privacyValid ? "Choose who can view this post."
    : !disclosureValid ? "Indicate whether this content promotes your brand, another brand, or both."
    : !draft.music_usage_confirmed || (branded && !draft.branded_content_policy_confirmed) ? "Confirm the applicable TikTok policies."
    : !draft.consent_confirmed ? "Confirm that you want to upload and post this video."
    : "";

  return (
    <div className="flex min-w-0 flex-col gap-5 text-sm" data-tiktok-publish-review>
      <p>Posting to <strong className="break-words">{creator.creator_nickname}</strong></p>
      {isVideo && previewUrlAllowed && (
        <MediaPreview refItem={{ ...review.media, type: "video" }} maxHeight={300}
          onVideoReady={previewReady} onPreviewError={previewError} />
      )}
      <p className="text-xs text-stone-500">Maximum video duration: {maxDuration} seconds.</p>
      <Textarea label="Caption" ariaLabel="TikTok caption" value={draft.caption} disabled={locked} error={captionError}
        onChange={(event) => update({ caption: event.target.value })} />
      <div className="flex flex-col gap-2">
        <span>Who can view this post?</span>
        <Select ariaLabel="TikTok privacy" value={draft.privacy_level} placeholder="Select privacy" disabled={locked}
          options={creator.privacy_level_options.map((level) => ({
            value: level, label: PRIVACY_LABELS[level], disabled: branded && level === "SELF_ONLY",
          }))} onChange={(privacy_level) => update({ privacy_level })} />
        {branded && <p className="text-xs text-stone-500">Branded content cannot be private. “Only me” is unavailable.</p>}
      </div>
      <fieldset className="flex min-w-0 flex-col gap-3">
        <legend className="mb-3 font-medium">Allow viewers to</legend>
        {([['comment', 'Comment'], ['duet', 'Duet'], ['stitch', 'Stitch']] as const).map(([kind, label]) => (
          <Checkbox key={kind} label={`${label}${creator[`${kind}_disabled`] ? " (disabled in TikTok settings)" : ""}`}
            checked={draft[`allow_${kind}`]} disabled={locked || creator[`${kind}_disabled`]}
            onChange={(value) => update({ [`allow_${kind}`]: value })} />
        ))}
      </fieldset>
      <div className="flex flex-col gap-3">
        <Checkbox label="Disclose commercial content" checked={draft.commercial_content} disabled={locked}
          onChange={(commercial_content) => update({ commercial_content, brand_organic_toggle: false, brand_content_toggle: false, branded_content_policy_confirmed: false })} />
        <p className="text-xs text-stone-500">Turn this on if the video promotes yourself, a brand, product, or service.</p>
        {draft.commercial_content && <>
          <Checkbox label="Your brand" checked={draft.brand_organic_toggle} disabled={locked}
            onChange={(brand_organic_toggle) => update({ brand_organic_toggle })} />
          <p className="text-xs text-stone-500">You are promoting yourself or your own business.</p>
          <Checkbox label="Branded content" checked={draft.brand_content_toggle} disabled={locked || draft.privacy_level === "SELF_ONLY"}
            onChange={(brand_content_toggle) => update({ brand_content_toggle, branded_content_policy_confirmed: false })} />
          <p className="text-xs text-stone-500">You are promoting another brand or a third party.{draft.privacy_level === "SELF_ONLY" ? " Choose a non-private audience to enable branded content." : ""}</p>
          {(draft.brand_organic_toggle || branded) && <p role="status">Your video will be labeled as “{branded ? "Paid partnership" : "Promotional content"}”.</p>}
          {!disclosureValid && <p role="status">Choose your brand, branded content, or both to continue.</p>}
        </>}
      </div>
      <div className="flex flex-col gap-3">
        <p>By posting, you agree to TikTok’s {branded && <><a className="underline" href="https://www.tiktok.com/legal/page/global/bc-policy/en" target="_blank" rel="noreferrer">Branded Content Policy</a> and </>}<a className="underline" href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank" rel="noreferrer">Music Usage Confirmation</a>.</p>
        <Checkbox label="I agree to TikTok’s Music Usage Confirmation" checked={draft.music_usage_confirmed} disabled={locked}
          onChange={(music_usage_confirmed) => update({ music_usage_confirmed })} />
        {branded && <Checkbox label="I agree to TikTok’s Branded Content Policy" checked={draft.branded_content_policy_confirmed} disabled={locked}
          onChange={(branded_content_policy_confirmed) => update({ branded_content_policy_confirmed })} />}
        <Checkbox label="I consent to upload and post this video to the TikTok account shown above with these settings."
          checked={draft.consent_confirmed} disabled={locked}
          onChange={(consent_confirmed) => setDraft((previous) => ({ ...previous, consent_confirmed }))} />
      </div>
      <p className="text-xs text-stone-500">TikTok may take a few minutes to process the video before it appears on your profile. Check the publishing result in this chat.</p>
      {disabledReason && <p role="status">{disabledReason}</p>}
      <div className="flex flex-wrap gap-2" title={disabledReason || undefined}>
        <Button variant="primary" disabled={locked || !valid} onClick={() => {
          if (locked || !valid || submitted.current) return;
          submitted.current = true;
          setSubmitting(true);
          onApprove(draft);
        }}>Post to TikTok</Button>
        <Button variant="outline" disabled={locked} onClick={onReject}>Cancel publishing</Button>
      </div>
    </div>
  );
}
