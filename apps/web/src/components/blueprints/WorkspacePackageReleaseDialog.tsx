/** Send a Blueprint file built in any environment to this Workspace as its package's next release. */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Textarea from "../ui/Textarea";
import { IconUpload } from "../icons";
import { api, type WorkspacePackageRelease } from "../../lib/api";
import { t } from "../../lib/i18n";

export default function WorkspacePackageReleaseDialog({ open, onClose, onPublished, workspaceId, needsAdoption }: {
  open: boolean;
  onClose: () => void;
  /** The release is recorded; the Workspace now takes it through the upgrade review. */
  onPublished: () => void;
  workspaceId: string;
  /** The Workspace has no package yet; the file becomes its package. */
  needsAdoption: boolean;
}) {
  const cache = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [notes, setNotes] = useState("");
  const [preview, setPreview] = useState<WorkspacePackageRelease | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function reset() { setFile(null); setNotes(""); setPreview(null); setError(""); }
  async function run(action: () => Promise<void>) {
    setBusy(true); setError("");
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : t("status.error")); } finally { setBusy(false); }
  }
  const options = { adopt: needsAdoption };
  const unmatched = preview?.adoption?.unmatched ?? [];

  return <Modal open={open} onClose={() => { if (!busy) { reset(); onClose(); } }} title={t("page.workspaces.package_release_title")}>
    <div className="space-y-4">
      <p className="text-sm text-stone-500">{t(needsAdoption ? "page.workspaces.package_release_adopt_body" : "page.workspaces.package_release_body")}</p>
      <label className="block cursor-pointer rounded-xl bg-stone-50 p-4">
        <input type="file" accept="application/json,.json,.manor" className="hidden" disabled={busy}
          onChange={(e) => { setFile(e.target.files?.[0] || null); setPreview(null); setError(""); }} />
        <span className="flex items-center gap-2 text-sm font-medium text-stone-800"><IconUpload size={15} />{file?.name || t("page.clients.update_choose_file")}</span>
        <span className="mt-1 block text-xs text-stone-500">{t("page.workspaces.package_release_file_help")}</span>
      </label>
      {preview && <div className="space-y-1 rounded-xl bg-stone-50 p-4 text-sm">
        <p className="mono text-xs text-stone-500">{preview.package.name}</p>
        <p className="font-medium text-stone-800">{preview.would_publish
          ? t("page.clients.update_preview_version", { version: preview.version || "" })
          : t("page.clients.update_unchanged", { version: preview.version || preview.current_version || "1.0.0" })}</p>
        {preview.would_publish && preview.level && <p className="text-stone-500">{t(`page.blueprints.upgrade_version_${preview.level}`)}</p>}
        {preview.components && <p className="mono text-xs text-stone-500">{t("page.clients.update_preview_components", {
          added: preview.components.added.length, changed: preview.components.changed.length, removed: preview.components.removed.length,
        })}</p>}
        {!!preview.reasons?.length && <ul className="list-disc pl-5 text-xs text-stone-500">{preview.reasons.slice(0, 6).map((reason) => <li key={reason}>{reason}</li>)}</ul>}
        {preview.adoption && <p className="text-stone-500">{t("page.workspaces.package_release_matched", { count: preview.adoption.matched.length })}</p>}
        {!!unmatched.length && <div className="text-xs text-stone-500">
          <p>{t("page.workspaces.package_release_unmatched")}</p>
          <ul className="mono list-disc pl-5">{unmatched.map((item) => <li key={item.component}>{item.component}</li>)}</ul>
        </div>}
      </div>}
      <Textarea label={t("page.clients.release_notes")} value={notes} rows={3} onChange={(e) => setNotes(e.target.value)} disabled={busy} />
      {!preview
        ? <Button loading={busy} disabled={!file} onClick={() => { if (file) void run(async () => {
            setPreview(await api.workspaces.publishPackageRelease(workspaceId, file, { ...options, dry_run: true }));
          }); }}>{t("page.clients.update_preview")}</Button>
        : <Button loading={busy} disabled={!preview.would_publish && !needsAdoption} onClick={() => { if (file) void run(async () => {
            await api.workspaces.publishPackageRelease(workspaceId, file, { ...options, release_notes: notes.trim() || undefined });
            await cache.invalidateQueries({ queryKey: ["workspace", workspaceId] });
            reset();
            onPublished();
          }); }}>{t("page.workspaces.package_release_publish")}</Button>}
      {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
    </div>
  </Modal>;
}
