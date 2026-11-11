/**
 * A free-text field's suggested values as cards to pick from. A long list (a
 * catalog of song styles or deck types) is browsed by group and search, shows
 * a first screenful with "Show all", and keeps the picked one in view. A value
 * with a sample can be heard before it is picked.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { t } from "../../lib/i18n";
import { ImagePreviewDialog } from "../file-preview/ImagePreview";
import { IconExpand, IconPause, IconPlay, IconSearch } from "../icons";
import type { WorkflowSchemaSuggestion } from "./workflowSchema";

const SHOWN = 8;
const SEARCH_FROM = 12;

export default function SuggestionGallery({ suggestions, value, label, disabled, onPick }: {
  suggestions: WorkflowSchemaSuggestion[];
  value: string;
  label: string;
  disabled?: boolean;
  onPick: (value: string) => void;
}) {
  const groups = useMemo(
    () => [...new Set(suggestions.map((item) => item.group || "").filter(Boolean))],
    [suggestions],
  );
  const [group, setGroup] = useState("");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [enlarged, setEnlarged] = useState<WorkflowSchemaSuggestion | null>(null);
  const needle = query.trim().toLowerCase();
  const matching = suggestions.filter((item) => (!group || item.group === group)
    && (!needle || `${item.label} ${item.detail} ${item.group || ""}`.toLowerCase().includes(needle)));
  // The picked value stays in view when the list is folded.
  const picked = matching.find((item) => item.value === value);
  const folded = matching.slice(0, SHOWN);
  const shown = open || matching.length <= SHOWN ? matching
    : picked && !folded.includes(picked) ? [picked, ...folded.slice(0, SHOWN - 1)] : folded;

  const audio = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  useEffect(() => () => audio.current?.pause(), []);
  const toggleSample = (item: WorkflowSchemaSuggestion) => {
    if (!item.sample) return;
    if (playing === item.value) {
      audio.current?.pause();
      setPlaying(null);
      return;
    }
    audio.current?.pause();
    const player = new Audio(item.sample);
    player.onended = () => setPlaying(null);
    audio.current = player;
    setPlaying(item.value);
    player.play().catch(() => setPlaying(null));
  };

  return <div className="suggestion-gallery">
    {(groups.length > 1 || suggestions.length > SEARCH_FROM) && <div className="suggestion-gallery__browse">
      {groups.length > 1 && <div className="suggestion-gallery__groups" role="group"
        aria-label={t("component.workflow_schema.groups", { field: label })}>
        {["", ...groups].map((name) => {
          // A group shows the picture of its first template, so it reads at a glance.
          const cover = name ? suggestions.find((item) => item.group === name && item.image)?.image : undefined;
          return <button key={name || "all"} type="button" aria-pressed={group === name}
            onClick={() => { setGroup(name); setOpen(false); }}>
            {cover && <img src={cover} alt="" loading="lazy" decoding="async" />}
            {name || t("component.workflow_schema.all_groups")}
          </button>;
        })}
      </div>}
      {suggestions.length > SEARCH_FROM && <label className="suggestion-gallery__search">
        <IconSearch size={13} />
        <input type="search" value={query} placeholder={t("component.workflow_schema.search_suggestions")}
          aria-label={t("component.workflow_schema.search_suggestions")}
          onChange={(event) => { setQuery(event.target.value); setOpen(false); }} />
      </label>}
    </div>}
    <div className="workflow-starter-input-suggestions" data-pictured={suggestions.some((item) => item.image) || undefined}
      role="group" aria-label={t("component.workflow_schema.suggestions", { field: label })}>
      {shown.map((item) => <div key={item.value} className="suggestion-gallery__item">
        <button type="button" className="workflow-starter-input-suggestion" aria-pressed={value === item.value}
          disabled={disabled} onClick={() => onPick(item.value)}>
          {item.image && <img src={item.image} alt="" loading="lazy" decoding="async" />}
          {item.label && <strong>{item.label}</strong>}
          <span>{item.detail}</span>
        </button>
        {/* Sibling of the pick button, not a child: the zoom never selects the card. */}
        {item.image && <div className="suggestion-gallery__thumb">
          <button type="button" className="suggestion-gallery__zoom"
            aria-label={t("component.workflow_schema.enlarge_image", { name: item.label || item.detail })}
            onClick={(event) => {
              // Focus the zoom control first, so the viewer hands focus back to it on close.
              event.currentTarget.focus();
              setEnlarged(item);
            }}>
            <IconExpand size={13} />
          </button>
        </div>}
        {item.sample && <button type="button" className="suggestion-gallery__sample" data-playing={playing === item.value || undefined}
          aria-label={t(playing === item.value ? "component.workflow_schema.stop_sample" : "component.workflow_schema.play_sample",
            { name: item.label || item.detail })}
          onClick={() => toggleSample(item)}>
          {playing === item.value ? <IconPause size={12} /> : <IconPlay size={12} />}
        </button>}
      </div>)}
      {matching.length === 0 && <p className="suggestion-gallery__empty">{t("component.workflow_schema.no_suggestion_match")}</p>}
    </div>
    {matching.length > SHOWN && <button type="button" className="suggestion-gallery__more" onClick={() => setOpen(!open)}>
      {open ? t("component.workflow_schema.show_fewer") : t("component.workflow_schema.show_all", { count: matching.length })}
    </button>}
    {enlarged?.image && <ImagePreviewDialog key={enlarged.value} src={enlarged.image} alt={enlarged.label || enlarged.detail}
      onClose={() => setEnlarged(null)} />}
  </div>;
}
