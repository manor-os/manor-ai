import Button from "../ui/Button";
import { tForLocale } from "../../lib/i18n";
import type { useResidentWebchat } from "../../lib/useResidentWebchat";

export default function ResidentAccountPanel({ resident, enabled, language, onNewConversation }: {
  resident: ReturnType<typeof useResidentWebchat>; enabled: boolean; language?: string | null; onNewConversation: () => void;
}) {
  const text = (key: string) => tForLocale(`webchat_resident.${key}`, language || "en");
  const busy = ["checking", "redirecting", "disconnecting"].includes(resident.phase);
  const verified = resident.phase === "verified" && resident.historyAllowed;
  return <section aria-label={text("title")} style={{ padding: "12px 16px", background: "var(--surface-muted)", display: "flex", flexDirection: "column", gap: 8, flexShrink: 0, maxHeight: 220, overflowY: "auto" }}>
    <strong style={{ fontSize: 13 }}>{text("title")}</strong>
    <p role="status" style={{ margin: 0, fontSize: 12 }}>{text(resident.phase)}</p>
    {resident.notice && <p role="alert" style={{ margin: 0, fontSize: 12 }}>{text(resident.notice)}</p>}
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      {verified ? <>
        <Button size="sm" variant="outline" disabled={resident.loadingItems} onClick={() => void resident.loadItems()}>{text("refresh")}</Button>
        <Button size="sm" variant="ghost" onClick={() => void resident.disconnect()}>{text("disconnect")}</Button>
      </> : <>
        {enabled && <Button size="sm" disabled={busy} loading={resident.phase === "redirecting"} onClick={() => void resident.start()}>{text("verify")}</Button>}
        {resident.phase === "unavailable" && <Button size="sm" variant="outline" onClick={() => void resident.check()}>{text("retry")}</Button>}
        {!resident.historyAllowed && !busy && <Button size="sm" variant="ghost" onClick={onNewConversation}>{text("new_chat")}</Button>}
      </>}
    </div>
    {verified && <>
      {resident.expiresAt && <small>{text("expires")} {new Date(resident.expiresAt).toLocaleTimeString(language || "en")}</small>}
      {resident.loadingItems ? <p role="status">{text("loading")}</p> : resident.itemsError ? <p role="alert">{text("load_failed")}</p> : resident.items.length === 0 ? <p>{text("empty")}</p> : <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12 }}>
        {resident.items.map(item => <li key={item.id} style={{ padding: "4px 0", overflowWrap: "anywhere" }}><strong>{item.title}</strong> — {item.status.replaceAll("_", " ")}</li>)}
      </ul>}
    </>}
  </section>;
}
