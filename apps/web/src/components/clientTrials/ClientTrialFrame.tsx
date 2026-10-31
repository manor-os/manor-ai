import { useEffect, useRef, useState } from "react";
import type { CollaboratorSession } from "../../lib/api";
import { t } from "../../lib/i18n";
import { connectEmbeddedFrame } from "../../embed/embedHandshake";
import type { EmbedKind } from "../../embed/embedContext";
import Button from "../ui/Button";
import EmptyState from "../ui/EmptyState";
import StatusPill from "../ui/StatusPill";
import { IconClose } from "../icons";
import { frameNotice, mintEnded, type FrameState } from "./clientTrialView";

const PHASE_STATUS: Record<string, string> = { trial: "in_progress", awaiting_acceptance: "pending", delivered: "completed" };

export function ClientTrialPhasePill({ phase }: { phase?: string | null }) {
  if (!phase) return null;
  return <StatusPill size="sm" status={PHASE_STATUS[phase]} label={t(`page.collaboration.phase_${phase}`)} />;
}

/**
 * The customer's Workspace inside the provider's own page: a same-origin frame
 * that boots the app in embedded mode with a scoped session minted here. The
 * provider never changes company and their own sign-in never enters the frame.
 * When access ends it stays here with one ended state until Close.
 */
export default function ClientTrialFrame({
  kind,
  refId,
  targetWorkspaceId,
  customerName,
  phase,
  mint,
  onClose,
}: {
  kind: EmbedKind;
  refId: string;
  targetWorkspaceId: string;
  customerName?: string | null;
  phase?: string | null;
  mint: () => Promise<CollaboratorSession>;
  onClose: () => void;
}) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [state, setState] = useState<FrameState>("connecting");
  const [company, setCompany] = useState(customerName || "");
  // The delivery moves on while the frame is open (acceptance): each mint and
  // each refetch of the trial list carries the current phase.
  const [currentPhase, setCurrentPhase] = useState(phase || null);
  useEffect(() => {
    if (phase) setCurrentPhase(phase);
  }, [phase]);
  const callbacks = useRef({ mint, onClose });
  callbacks.current = { mint, onClose };

  useEffect(() => {
    setState("connecting");
    return connectEmbeddedFrame({
      frame: () => frameRef.current?.contentWindow ?? null,
      kind,
      refId,
      mint: async () => {
        const session = await callbacks.current.mint();
        if (session.customer_name) setCompany(session.customer_name);
        if (session.phase) setCurrentPhase(session.phase);
        return session;
      },
      isEnded: mintEnded,
      onConnected: () => setState("ready"),
      onFailed: (error) => setState(mintEnded(error) ? "ended" : "unavailable"),
      onEnded: () => setState("ended"),
      onClose: () => callbacks.current.onClose(),
    });
  }, [kind, refId]);

  const title = t("page.client_trials.header", { company: company || "—" });
  const notice = frameNotice(state);
  const src = `/embed/${kind}/${encodeURIComponent(refId)}/workspaces/${encodeURIComponent(targetWorkspaceId)}`;
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col gap-2">
      <div className="flex shrink-0 items-center gap-2">
        <h2 className="min-w-0 truncate text-sm font-semibold text-stone-800">{title}</h2>
        <ClientTrialPhasePill phase={currentPhase} />
        <span className="flex-1" />
        <Button variant="ghost" size="sm" onClick={onClose} ariaLabel={t("page.client_trials.close")} title={t("page.client_trials.close")}>
          <IconClose size={16} />
        </Button>
      </div>
      {notice ? (
        // Never a server message here: one state, and the way out.
        <div role="status" className="flex min-h-0 flex-1 items-center justify-center rounded-2xl border border-stone-200 bg-white">
          <EmptyState
            title={t(`page.client_trials.${notice}`)}
            action={<Button variant="outline" onClick={onClose}>{t("page.client_trials.close")}</Button>}
          />
        </div>
      ) : (
        <iframe
          ref={frameRef}
          src={src}
          title={title}
          className="min-h-0 w-full flex-1 rounded-2xl border border-stone-200 bg-white"
        />
      )}
    </div>
  );
}
