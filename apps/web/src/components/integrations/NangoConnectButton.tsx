/**
 * NangoConnectButton — opens Nango's hosted Connect popup so the user
 * can OAuth into a SaaS platform (Twitter / Slack / Notion / …) without
 * leaving manor-os. After the popup closes we ask the backend to sync
 * the new Connection into the local ``integrations`` table.
 *
 * Renders nothing UI-wise except the button itself; embed inside the
 * Integrations page or per-platform card.
 */
import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useToastStore } from "../../stores/toast";
import Button from "../ui/Button";
import { t } from "../../lib/i18n";


interface Props {
  /** Restrict the popup to these Nango integration ids (e.g. ['twitter']).
   *  Leave undefined for "any platform". */
  providerConfigKeys?: string[];
  /** Visible button label override. */
  label?: string;
  variant?: "primary" | "outline" | "ghost";
  size?: "sm" | "md" | "lg";
  /** Called after a successful sync. */
  onConnected?: (providers: string[]) => void;
}

const POPUP_FEATURES = "popup=yes,width=560,height=720,scrollbars=yes";

export default function NangoConnectButton({
  providerConfigKeys,
  label,
  variant = "primary",
  size = "sm",
  onConnected,
}: Props) {
  const toast = useToastStore();
  const queryClient = useQueryClient();
  const [popupRef, setPopupRef] = useState<Window | null>(null);
  const pendingPopupRef = useRef<Window | null>(null);

  const beginPopupPolling = (popup: Window) => {
    const tick = window.setInterval(() => {
      if (popup.closed) {
        window.clearInterval(tick);
        pendingPopupRef.current = null;
        setPopupRef(null);
        sync.mutate();
      }
    }, 600);
  };

  const start = useMutation({
    mutationFn: () => api.integrations.nango.startConnect(providerConfigKeys),
    onSuccess: ({ nango_connect_url }) => {
      const popup = pendingPopupRef.current && !pendingPopupRef.current.closed
        ? pendingPopupRef.current
        : window.open("about:blank", "nango_connect", POPUP_FEATURES);
      if (!popup) {
        toast.error(
          t("component.nango_connect_button.popup_blocked"),
          t("component.nango_connect_button.allow_popups"),
        );
        return;
      }
      popup.location.href = nango_connect_url;
      setPopupRef(popup);
      beginPopupPolling(popup);
    },
    onError: (err: Error) => {
      const popup = pendingPopupRef.current;
      if (popup && !popup.closed) popup.close();
      pendingPopupRef.current = null;
      setPopupRef(null);
      toast.error(t("component.nango_connect_button.could_not_start_nango_connect"), err.message);
    },
  });

  const sync = useMutation({
    mutationFn: () => api.integrations.nango.sync(),
    onSuccess: ({ upserted, providers }) => {
      // ``["integrations"]`` was the old query key; the page uses
      // ``["mcp-servers"]`` now. Invalidating the dead key meant the
      // Facebook card stayed in "Connect" state after a successful
      // OAuth — sync ran, toast fired, but the card didn't reflect
      // ``entity_connected=true`` until the user navigated away and
      // back (the mcp-servers query has staleTime 60s + no refetch
      // interval). Invalidate the real key here.
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      if (upserted === 0) {
        toast.info(t("component.nango_connect_button.no_new_connection"), t("component.nango_connect_button.popup_closed_without_authorizing"));
      } else {
        toast.success(
          t(upserted === 1 ? "component.nango_connect_button.connected_platform" : "component.nango_connect_button.connected_platforms").replace("{count}", String(upserted)),
          providers.join(" · "),
        );
        onConnected?.(providers);
      }
    },
    onError: (err: Error) => {
      toast.error(t("component.nango_connect_button.could_not_sync_connections"), err.message);
    },
  });

  const isWorking = start.isPending || sync.isPending || popupRef !== null;

  return (
    <Button
      variant={variant}
      size={size}
      onClick={() => {
        const popup = window.open("about:blank", "nango_connect", POPUP_FEATURES);
        if (!popup) {
          toast.error(
            t("component.nango_connect_button.popup_blocked"),
            t("component.nango_connect_button.allow_popups"),
          );
          return;
        }
        pendingPopupRef.current = popup;
        setPopupRef(popup);
        start.mutate();
      }}
      loading={isWorking}
      disabled={isWorking}
    >
      {label
        || (providerConfigKeys && providerConfigKeys.length === 1
          ? `Connect ${providerConfigKeys[0]}`
          : t("component.nango_connect_button.connect_via_nango"))}
    </Button>
  );
}
