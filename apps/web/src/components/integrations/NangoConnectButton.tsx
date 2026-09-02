/**
 * NangoConnectButton — opens Nango's hosted Connect popup so the user
 * can OAuth into a SaaS platform (Twitter / Slack / Notion / …) without
 * leaving manor-os. After the popup closes we ask the backend to sync
 * the new Connection into the local ``integrations`` table.
 *
 * WhatsApp Embedded Signup reports the selected WABA and phone number back to
 * the opener; those identifiers are forwarded to the sync endpoint for
 * server-side verification.
 */
import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useToastStore } from "../../stores/toast";
import Button from "../ui/Button";
import { t } from "../../lib/i18n";
import { INTEGRATION_CATALOG_QUERY_KEY } from "../../lib/integrationCatalog";


interface Props {
  /** Restrict the popup to these Nango integration ids (e.g. ['twitter']).
   *  Leave undefined for "any platform". */
  providerConfigKeys?: string[];
  /** Existing Manor Integration row to replace after OAuth succeeds. */
  replaceIntegrationId?: string;
  /** Visible button label override. */
  label?: string;
  variant?: "primary" | "outline" | "ghost";
  size?: "sm" | "md" | "lg";
  /** Called after a successful sync. */
  onConnected?: (providers: string[]) => void;
}

const POPUP_FEATURES = "popup=yes,width=560,height=720,scrollbars=yes";
const META_EMBEDDED_SIGNUP_ORIGINS = new Set([
  "https://www.facebook.com",
  "https://web.facebook.com",
]);

interface NangoConnectOptions {
  providerConfigKeys?: string[];
  replaceIntegrationId?: string;
  onConnected?: (providers: string[]) => void;
}

type WhatsAppEmbeddedSignupOutcome =
  | {
      event: "FINISH";
      whatsapp_waba_id: string;
      whatsapp_phone_number_id: string;
    }
  | { event: "CANCEL" }
  | { event: "ERROR"; detail: string };

export function useNangoConnect({
  providerConfigKeys,
  replaceIntegrationId,
  onConnected,
}: NangoConnectOptions) {
  const toast = useToastStore();
  const queryClient = useQueryClient();
  const [popupRef, setPopupRef] = useState<Window | null>(null);
  const isWhatsApp = providerConfigKeys?.includes("whatsapp") === true;
  const pendingPopupRef = useRef<Window | null>(null);
  const expectedConnectionIdRef = useRef<string | null>(null);
  const expectedProviderConfigKeyRef = useRef<string | null>(null);
  const whatsappEmbeddedSignupOutcomeRef = useRef<
    WhatsAppEmbeddedSignupOutcome | null
  >(null);
  const mountedRef = useRef(true);
  const pollingRef = useRef<number | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (pollingRef.current !== null) window.clearInterval(pollingRef.current);
      pollingRef.current = null;
      const popup = pendingPopupRef.current;
      pendingPopupRef.current = null;
      expectedConnectionIdRef.current = null;
      expectedProviderConfigKeyRef.current = null;
      whatsappEmbeddedSignupOutcomeRef.current = null;
      if (popup && !popup.closed) popup.close();
    };
  }, []);

  useEffect(() => {
    if (!isWhatsApp) return;
    const handleEmbeddedSignupMessage = (event: MessageEvent) => {
      if (
        !pendingPopupRef.current
        || event.source !== pendingPopupRef.current
        || !META_EMBEDDED_SIGNUP_ORIGINS.has(event.origin)
      ) return;
      let payload: unknown = event.data;
      if (typeof payload === "string") {
        try {
          payload = JSON.parse(payload);
        } catch {
          return;
        }
      }
      if (!payload || typeof payload !== "object") return;
      const embeddedSignup = payload as {
        type?: unknown;
        event?: unknown;
        data?: {
          waba_id?: unknown;
          phone_number_id?: unknown;
          error_message?: unknown;
        };
      };
      if (embeddedSignup.type !== "WA_EMBEDDED_SIGNUP") return;
      if (embeddedSignup.event === "CANCEL") {
        whatsappEmbeddedSignupOutcomeRef.current = { event: "CANCEL" };
        return;
      }
      if (embeddedSignup.event === "ERROR") {
        const detail = typeof embeddedSignup.data?.error_message === "string"
          ? embeddedSignup.data.error_message.trim()
          : "";
        whatsappEmbeddedSignupOutcomeRef.current = { event: "ERROR", detail };
        return;
      }
      if (embeddedSignup.event !== "FINISH") return;
      const wabaId = typeof embeddedSignup.data?.waba_id === "string"
        ? embeddedSignup.data.waba_id.trim()
        : "";
      const phoneNumberId = typeof embeddedSignup.data?.phone_number_id === "string"
        ? embeddedSignup.data.phone_number_id.trim()
        : "";
      if (!wabaId || !phoneNumberId) return;
      whatsappEmbeddedSignupOutcomeRef.current = {
        event: "FINISH",
        whatsapp_waba_id: wabaId,
        whatsapp_phone_number_id: phoneNumberId,
      };
    };
    window.addEventListener("message", handleEmbeddedSignupMessage);
    return () => window.removeEventListener("message", handleEmbeddedSignupMessage);
  }, [isWhatsApp]);

  const beginPopupPolling = (popup: Window) => {
    const tick = window.setInterval(() => {
      if (popup.closed) {
        window.clearInterval(tick);
        pollingRef.current = null;
        pendingPopupRef.current = null;
        setPopupRef(null);
        const expectedConnectionId = expectedConnectionIdRef.current;
        const expectedProviderConfigKey = expectedProviderConfigKeyRef.current;
        const whatsappOutcome = whatsappEmbeddedSignupOutcomeRef.current;
        expectedConnectionIdRef.current = null;
        expectedProviderConfigKeyRef.current = null;
        whatsappEmbeddedSignupOutcomeRef.current = null;
        if (isWhatsApp && whatsappOutcome?.event !== "FINISH") {
          if (whatsappOutcome?.event === "ERROR") {
            toast.error(
              t("component.nango_connect_button.whatsapp_signup_failed"),
              whatsappOutcome.detail,
            );
          } else {
            toast.info(
              t("component.nango_connect_button.whatsapp_signup_cancelled"),
              t("component.nango_connect_button.popup_closed_without_authorizing"),
            );
          }
          return;
        }
        const whatsappEmbeddedSignup = whatsappOutcome?.event === "FINISH" ? {
          whatsapp_waba_id: whatsappOutcome.whatsapp_waba_id,
          whatsapp_phone_number_id: whatsappOutcome.whatsapp_phone_number_id,
        } : {};
        if (expectedConnectionId && expectedProviderConfigKey) {
          sync.mutate({
            expected_connection_id: expectedConnectionId,
            expected_provider_config_key: expectedProviderConfigKey,
            replace_integration_id: replaceIntegrationId,
            ...whatsappEmbeddedSignup,
          });
        }
      }
    }, 600);
    pollingRef.current = tick;
  };

  const start = useMutation({
    mutationFn: () =>
      api.integrations.nango.startConnect(
        providerConfigKeys,
        replaceIntegrationId,
      ),
    onSuccess: ({ nango_connect_url, connection_id, provider_config_key }) => {
      // Mutation callbacks can still run after the setup UI has been dismissed.
      if (!mountedRef.current) return;
      expectedConnectionIdRef.current = connection_id;
      expectedProviderConfigKeyRef.current = provider_config_key;
      const popup = pendingPopupRef.current && !pendingPopupRef.current.closed
        ? pendingPopupRef.current
        : window.open("about:blank", "_blank", POPUP_FEATURES);
      if (!popup) {
        toast.error(
          t("component.nango_connect_button.popup_blocked"),
          t("component.nango_connect_button.allow_popups"),
        );
        pendingPopupRef.current = null;
        expectedConnectionIdRef.current = null;
        expectedProviderConfigKeyRef.current = null;
        setPopupRef(null);
        return;
      }
      pendingPopupRef.current = popup;
      popup.location.href = nango_connect_url;
      setPopupRef(popup);
      beginPopupPolling(popup);
    },
    onError: (err: Error) => {
      if (!mountedRef.current) return;
      const popup = pendingPopupRef.current;
      if (popup && !popup.closed) popup.close();
      pendingPopupRef.current = null;
      expectedConnectionIdRef.current = null;
      expectedProviderConfigKeyRef.current = null;
      whatsappEmbeddedSignupOutcomeRef.current = null;
      setPopupRef(null);
      toast.error(t("component.nango_connect_button.could_not_start_nango_connect"), err.message);
    },
  });

  const sync = useMutation({
    mutationFn: (data: {
      expected_connection_id: string;
      expected_provider_config_key: string;
      replace_integration_id?: string;
      whatsapp_waba_id?: string;
      whatsapp_phone_number_id?: string;
    }) => api.integrations.nango.sync(data),
  onSuccess: ({
    upserted,
    providers,
      readiness_code,
      provisioning_pending,
      provisioning_detail,
    }) => {
      // An already-sent sync can persist a connection after dismissal. Refresh
      // the catalog, but never let that old session update a newer setup UI.
      queryClient.invalidateQueries({ queryKey: INTEGRATION_CATALOG_QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: ["channel-bindings"] });
      if (!mountedRef.current) return;
      if (provisioning_pending) {
        toast.info(
          readiness_code === "phone_not_registered"
            ? t("component.nango_connect_button.whatsapp_pin_required")
            : t("component.nango_connect_button.whatsapp_provisioning_pending"),
          provisioning_detail || "",
        );
        return;
      }
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
      if (!mountedRef.current) return;
      toast.error(t("component.nango_connect_button.could_not_sync_connections"), err.message);
    },
  });

  const isWorking = start.isPending || sync.isPending || popupRef !== null;

  const begin = () => {
    if (!mountedRef.current || isWorking || pendingPopupRef.current) return;
    whatsappEmbeddedSignupOutcomeRef.current = null;
    // Each controller owns its window; another connect flow must not reuse it.
    const popup = window.open("about:blank", "_blank", POPUP_FEATURES);
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
  };

  return { begin, isWorking };
}

export default function NangoConnectButton({
  providerConfigKeys,
  replaceIntegrationId,
  label,
  variant = "primary",
  size = "sm",
  onConnected,
}: Props) {
  const { begin, isWorking } = useNangoConnect({
    providerConfigKeys,
    replaceIntegrationId,
    onConnected,
  });

  return (
    <Button
      variant={variant}
      size={size}
      onClick={begin}
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
