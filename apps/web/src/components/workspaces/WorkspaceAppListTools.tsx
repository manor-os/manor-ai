import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { useToastStore } from "../../stores/toast";
import { IconBookmark, IconClose, IconDownload } from "../icons";
import AnchoredPopover from "../ui/AnchoredPopover";
import Button from "../ui/Button";
import Input from "../ui/Input";
import Select from "../ui/Select";
import { SAVED_VIEWS_PREFERENCE, savedViewsOf, withSavedViews, type SavedView } from "./workspaceAppCollection";

/** What a person does with a whole list: pick a grouping, save the list as they set it, export it, act on ticked rows. */

export type ListState = Omit<SavedView, "id" | "name">;

function sameState(view: SavedView, state: ListState): boolean {
  return JSON.stringify([view.filters, view.q, view.group, view.sort]) === JSON.stringify([state.filters, state.q, state.group, state.sort]);
}

/** A person's own tabs over a list: "All", then each view they saved (its filters, search, grouping, sort).
 *  They follow the person to every device: they are kept with their account preferences. */
export function SavedViewTabs({ listKey, state, initial, onApply }: { listKey: string; state: ListState; initial: ListState; onApply: (view: ListState) => void }) {
  const queryClient = useQueryClient();
  const preferences = useQuery({ queryKey: ["preferences"], queryFn: () => api.admin.getPreferences(), staleTime: 60_000 });
  const views = savedViewsOf(preferences.data?.[SAVED_VIEWS_PREFERENCE], listKey);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const active = views.find((view) => sameState(view, state));
  // The list as the page opens it is "All"; anything a person changed from it can be saved.
  const plain = sameState({ id: "", name: "", ...initial }, state);
  const save = async (next: (current: SavedView[]) => SavedView[]) => {
    setBusy(true);
    try {
      // Read again right before writing: another tab or device may have saved a view meanwhile.
      const fresh = await api.admin.getPreferences();
      const stored = fresh?.[SAVED_VIEWS_PREFERENCE];
      const updated = await api.admin.updatePreferences({
        [SAVED_VIEWS_PREFERENCE]: withSavedViews(stored, listKey, next(savedViewsOf(stored, listKey))),
      });
      queryClient.setQueryData(["preferences"], updated);
      return true;
    } catch (error) {
      useToastStore.getState().error(t("component.workspace_app_view.views_failed"), error instanceof Error ? error.message : undefined);
      return false;
    } finally {
      setBusy(false);
    }
  };
  const submit = async (event: FormEvent, close: () => void) => {
    event.preventDefault();
    const label = name.trim().slice(0, 40);
    if (!label) return;
    const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    if (await save((current) => [...current.filter((view) => view.name !== label), { id, name: label, ...state }])) {
      setName("");
      close();
    }
  };
  // Nothing saved and nothing to save yet: the list's own controls are enough.
  if (preferences.isError || (!views.length && plain)) return null;
  return <div className="workspace-app-data__views" role="tablist" aria-label={t("component.workspace_app_view.views")}>
    <button type="button" role="tab" aria-selected={!active && plain} onClick={() => onApply(initial)}>
      {t("component.workspace_app_view.views_all")}</button>
    {views.map((view) => <span key={view.id} className="workspace-app-data__view-tab">
      <button type="button" role="tab" aria-selected={active?.id === view.id} onClick={() => onApply(view)}>{view.name}</button>
      <button type="button" className="workspace-app-data__view-remove" disabled={busy}
        aria-label={t("component.workspace_app_view.views_remove", { name: view.name })}
        onClick={() => void save((current) => current.filter((each) => each.id !== view.id))}><IconClose size={11} /></button>
    </span>)}
    {!active && !plain && views.length < 12 && <AnchoredPopover ariaLabel={t("component.workspace_app_view.views_save")} width={260} align="left"
      trigger={<Button size="sm" variant="ghost"><IconBookmark size={13} />{t("component.workspace_app_view.views_save")}</Button>}>
      {({ close }) => <form className="workspace-app-data__view-form" onSubmit={(event) => void submit(event, close)}>
        <Input id={`save-view-${listKey}`} label={t("component.workspace_app_view.views_name")} value={name} maxLength={40} autoFocus onChange={(event) => setName(event.target.value)}
          placeholder={t("component.workspace_app_view.views_name_placeholder")} />
        <Button type="submit" size="sm" loading={busy} disabled={!name.trim()}>{t("component.workspace_app_view.views_save")}</Button>
      </form>}
    </AnchoredPopover>}
  </div>;
}

/** Which of the list's groupings applies, or none. */
export function GroupPicker({ choices, value, onChange }: { choices: Array<{ label: string }>; value: number; onChange: (value: number) => void }) {
  return <Select appearance="ghost" value={String(value)} aria-label={t("component.workspace_app_view.group_by")}
    options={[{ value: "-1", label: t("component.workspace_app_view.group_none") },
      ...choices.map((choice, index) => ({ value: String(index), label: t("component.workspace_app_view.group_by_field", { field: choice.label }) }))]}
    onChange={(next) => onChange(Number(next))} />;
}

/** A list's CSV, saved through the browser's own download. */
export function downloadCsv(text: string, fileName: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export function ExportButton({ onExport, label }: { onExport: () => Promise<void>; label?: string }) {
  const [busy, setBusy] = useState(false);
  return <Button size="sm" variant="ghost" loading={busy} disabled={busy} title={label || t("component.workspace_app_view.export_csv")}
    onClick={async () => {
      setBusy(true);
      try {
        await onExport();
      } catch {
        useToastStore.getState().error(t("component.workspace_app_view.export_failed"));
      } finally {
        setBusy(false);
      }
    }}><IconDownload size={14} />{label || t("component.workspace_app_view.export_csv")}</Button>;
}

/** What the ticked rows add up to, and what can be done with them here. */
export function SelectionBar({ count, totals, onClear, onExport }: {
  count: number; totals: Array<{ label: string; text: string }>; onClear: () => void; onExport?: () => Promise<void>;
}) {
  if (!count) return null;
  return <div className="workspace-app-data__selection" role="status">
    <strong>{t("component.workspace_app_view.selected_count", { count })}</strong>
    {totals.map((total) => <span key={total.label}><b>{total.label}</b> <span className="mono">{total.text}</span></span>)}
    {onExport && <ExportButton onExport={onExport} label={t("component.workspace_app_view.export_selected")} />}
    <Button size="sm" variant="ghost" onClick={onClear}>{t("component.workspace_app_view.selected_clear")}</Button>
  </div>;
}
