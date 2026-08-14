/**
 * SitePublishAction — one icon button in the file viewer's action row.
 *
 * Renders nothing unless the open document is publishable (a folder whose root
 * has index.html, or a standalone .html file — the backend decides). Not
 * published yet: the button opens one publish-and-connect confirmation.
 * Already published: the button is active and opens the site drawer, which
 * owns everything else (address, republish,
 * custom domain, offline). Keeping it to a single control in the row the file
 * viewer already has avoids a second strip of actions competing with it.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  type SiteAnalytics,
  type SiteAutoConnectionPlan,
  type SiteConnections,
  type SiteInfo,
  type SitePublishResult,
} from "../lib/api";
import { t } from "../lib/i18n";
import type { Document, Workspace } from "../lib/types";
import Button from "./ui/Button";
import Checkbox from "./ui/Checkbox";
import Input from "./ui/Input";
import Select from "./ui/Select";
import {
  IconCheck,
  IconCopy,
  IconExternalLink,
  IconEye,
  IconEyeOff,
  IconGlobe,
  IconRefresh,
} from "./icons";
import { closeDetail, openDetail, useDetailStore } from "../stores/detail";

function CopyableUrl({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2 rounded-lg bg-stone-100 px-3 py-2">
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        className="mono flex-1 truncate text-xs text-manor-700 hover:underline"
      >
        {url}
      </a>
      <button
        type="button"
        aria-label={t("sites.copy")}
        className="text-stone-400 hover:text-stone-600"
        onClick={() => {
          navigator.clipboard?.writeText(url);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
      >
        {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
      </button>
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        className="text-stone-400 hover:text-stone-600"
        aria-label={t("sites.visit")}
      >
        <IconExternalLink size={14} />
      </a>
    </div>
  );
}


function PublishConfirmation({
  plan,
}: {
  plan: SiteAutoConnectionPlan | null;
}) {
  const actionLabel = (action: string) => {
    if (action === "reuse") return t("sites.autoReuse");
    if (action === "enable") return t("sites.autoEnable");
    return t("sites.autoCreate");
  };
  const rows = plan?.eligible
    ? [
        {
          key: "customer-service",
          label: t("sites.customerService"),
          action: plan.actions.customer_service,
          visible: true,
        },
        {
          key: "lead",
          label: t("sites.leadFlow"),
          action: plan.actions.lead_flow,
          visible: plan.features.lead_forms > 0,
        },
        {
          key: "subscription",
          label: t("sites.subscriptionFlow"),
          action: plan.actions.subscription_flow,
          visible: plan.features.subscription_forms > 0,
        },
        {
          key: "analytics",
          label: t("sites.firstPartyAnalytics"),
          action: plan.actions.analytics,
          visible: true,
        },
      ].filter((row) => row.visible)
    : [];

  return (
    <div className="space-y-4">
      {plan?.eligible ? (
        <>
          <p className="text-sm leading-6 text-stone-600">
            {t("sites.publishConfirmDescription", {
              workspace: plan.workspace_name || t("sites.workspace"),
            })}
          </p>
          <div>
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-stone-500">
              {t("sites.autoConnectionReady")}
            </div>
            <div className="divide-y divide-stone-100 rounded-lg border border-stone-200 bg-white">
              {rows.map((row) => (
                <div key={row.key} className="flex items-center gap-3 px-3 py-2.5">
                  <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-manor-50 text-manor-700">
                    <IconCheck size={14} />
                  </span>
                  <span className="min-w-0 flex-1 text-sm text-stone-700">{row.label}</span>
                  <span className="shrink-0 text-xs font-medium text-manor-700">
                    {actionLabel(row.action)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </>
      ) : (
        <p className="rounded-lg bg-stone-50 px-3 py-2.5 text-sm leading-6 text-stone-600">
          {t("sites.publishConfirmFallback")}
        </p>
      )}
      <p className="text-xs leading-5 text-stone-500">{t("sites.publishPublicWarning")}</p>
    </div>
  );
}


type SiteConnectionDraft = Omit<SiteConnections, "site_id">;

function initialConnections(site: SiteInfo): SiteConnectionDraft {
  return {
    workspace_id: site.workspace_id || null,
    customer_service_channel_config_id:
      site.connections?.customer_service_channel_config_id || null,
    subscription_workflow_binding_id:
      site.connections?.subscription_workflow_binding_id || null,
    lead_workflow_binding_id: site.connections?.lead_workflow_binding_id || null,
    analytics_enabled: site.connections?.analytics_enabled ?? true,
  };
}

function SiteConnectionsPanel({
  site,
  onSiteChange,
}: {
  site: SiteInfo;
  onSiteChange: (site: SiteInfo) => void;
}) {
  const [draft, setDraft] = useState<SiteConnectionDraft>(() => initialConnections(site));
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [channels, setChannels] = useState<any[]>([]);
  const [bindings, setBindings] = useState<any[]>([]);
  const [analytics, setAnalytics] = useState<SiteAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingWorkspace, setLoadingWorkspace] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      api.sites.getConnections(site.id),
      api.workspaces.list(),
      api.sites.analytics(site.id, 30),
    ])
      .then(([connections, workspaceItems, siteAnalytics]) => {
        if (cancelled) return;
        const { site_id: _siteId, ...next } = connections;
        setDraft(next);
        setWorkspaces(workspaceItems);
        setAnalytics(siteAnalytics);
      })
      .catch((e: any) => {
        if (!cancelled) setError(e?.message || String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [site.id]);

  useEffect(() => {
    const workspaceId = draft.workspace_id;
    if (!workspaceId) {
      setChannels([]);
      setBindings([]);
      return;
    }
    let cancelled = false;
    setLoadingWorkspace(true);
    Promise.all([
      api.workspaces.channels(workspaceId),
      api.workflows.listBindings({ workspace_id: workspaceId }),
    ])
      .then(([channelItems, bindingItems]) => {
        if (cancelled) return;
        setChannels(
          channelItems.filter(
            (channel) => channel.channel_type === "webchat" && channel.public_token,
          ),
        );
        setBindings(
          bindingItems.filter((binding) => binding.enabled && binding.status === "active"),
        );
      })
      .catch((e: any) => {
        if (!cancelled) setError(e?.message || String(e));
      })
      .finally(() => {
        if (!cancelled) setLoadingWorkspace(false);
      });
    return () => {
      cancelled = true;
    };
  }, [draft.workspace_id]);

  const workspaceOptions = [
    { value: "", label: t("sites.connectionNone") },
    ...workspaces.map((workspace) => ({ value: workspace.id, label: workspace.name })),
  ];
  const channelOptions = [
    { value: "", label: t("sites.connectionNone") },
    ...channels.map((channel) => ({
      value: channel.id,
      label: channel.name || t("sites.customerService"),
    })),
  ];
  const flowOptions = [
    { value: "", label: t("sites.connectionNone") },
    ...bindings.map((binding) => ({
      value: binding.id,
      label: binding.name || binding.workflow_id,
    })),
  ];

  const save = async () => {
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const updated = await api.sites.updateConnections(site.id, draft);
      onSiteChange(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-stone-500">
        {t("sites.manorConnections")}
      </div>
      <div className="space-y-3 rounded-lg bg-stone-50 p-3">
        <p className="text-xs leading-5 text-stone-500">{t("sites.connectionsHint")}</p>

        <div className="block space-y-1.5">
          <span className="text-xs font-medium text-stone-600">{t("sites.workspace")}</span>
          <Select
            value={draft.workspace_id || ""}
            onChange={(value) => {
              setSaved(false);
              setDraft((current) => ({
                workspace_id: value || null,
                customer_service_channel_config_id: null,
                subscription_workflow_binding_id: null,
                lead_workflow_binding_id: null,
                analytics_enabled: current.analytics_enabled,
              }));
            }}
            options={workspaceOptions}
            disabled={loading || saving}
            ariaLabel={t("sites.workspace")}
            style={{ width: "100%" }}
            buttonStyle={{ width: "100%" }}
          />
        </div>

        {draft.workspace_id && (
          <>
            <div className="block space-y-1.5">
              <span className="text-xs font-medium text-stone-600">
                {t("sites.customerService")}
              </span>
              <Select
                value={draft.customer_service_channel_config_id || ""}
                onChange={(value) =>
                  setDraft((current) => ({
                    ...current,
                    customer_service_channel_config_id: value || null,
                  }))
                }
                options={channelOptions}
                disabled={loadingWorkspace || saving}
                ariaLabel={t("sites.customerService")}
                style={{ width: "100%" }}
                buttonStyle={{ width: "100%" }}
              />
              {!loadingWorkspace && channels.length === 0 && (
                <span className="block text-xs leading-5 text-amber-700">
                  {t("sites.noWebchat")}
                </span>
              )}
            </div>

            <div className="block space-y-1.5">
              <span className="text-xs font-medium text-stone-600">
                {t("sites.subscriptionFlow")}
              </span>
              <Select
                value={draft.subscription_workflow_binding_id || ""}
                onChange={(value) =>
                  setDraft((current) => ({
                    ...current,
                    subscription_workflow_binding_id: value || null,
                  }))
                }
                options={flowOptions}
                disabled={loadingWorkspace || saving}
                ariaLabel={t("sites.subscriptionFlow")}
                style={{ width: "100%" }}
                buttonStyle={{ width: "100%" }}
              />
            </div>

            <div className="block space-y-1.5">
              <span className="text-xs font-medium text-stone-600">
                {t("sites.leadFlow")}
              </span>
              <Select
                value={draft.lead_workflow_binding_id || ""}
                onChange={(value) =>
                  setDraft((current) => ({
                    ...current,
                    lead_workflow_binding_id: value || null,
                  }))
                }
                options={flowOptions}
                disabled={loadingWorkspace || saving}
                ariaLabel={t("sites.leadFlow")}
                style={{ width: "100%" }}
                buttonStyle={{ width: "100%" }}
              />
              {!loadingWorkspace && bindings.length === 0 && (
                <span className="block text-xs leading-5 text-amber-700">
                  {t("sites.noFlows")}
                </span>
              )}
            </div>
          </>
        )}

        <Checkbox
          checked={draft.analytics_enabled}
          onChange={(checked) =>
            setDraft((current) => ({ ...current, analytics_enabled: checked }))
          }
          disabled={loading || saving}
          size="sm"
          label={t("sites.firstPartyAnalytics")}
        />

        {analytics && (
          <div className="grid grid-cols-4 gap-2 rounded-lg bg-white p-2 text-center">
            {[
              [t("sites.pageViews"), analytics.page_views],
              [t("sites.visitors"), analytics.unique_sessions],
              [t("sites.forms"), analytics.form_submissions],
              [t("sites.chats"), analytics.chat_opens],
            ].map(([label, value]) => (
              <div key={String(label)} className="min-w-0">
                <div className="text-sm font-semibold text-stone-700">
                  {Number(value).toLocaleString()}
                </div>
                <div className="truncate text-[10px] text-stone-400">{label}</div>
              </div>
            ))}
          </div>
        )}

        <div className="flex items-center justify-between gap-3">
          <span className={`text-xs ${error ? "text-red-600" : "text-manor-700"}`}>
            {error || (saved ? t("sites.connectionsSaved") : "")}
          </span>
          <Button size="sm" disabled={loading || saving} onClick={save}>
            {saving ? t("sites.savingConnections") : t("action.save")}
          </Button>
        </div>
      </div>
    </div>
  );
}

function SiteDrawerBody({
  site,
  excluded,
  publishError,
  publishing,
  onRepublish,
  onSiteChange,
}: {
  site: SiteInfo;
  excluded: SitePublishResult["excluded"];
  publishError: string | null;
  publishing: boolean;
  onRepublish: () => void;
  onSiteChange: (s: SiteInfo) => void;
}) {
  // Domain input state lives here, not in the parent: the parent re-creates
  // this element whenever the site changes, and lifting the value up would
  // rebuild the drawer on every keystroke.
  const [domain, setDomain] = useState(site.custom_domain || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bound = !!site.custom_domain && site.custom_domain === domain.trim().toLowerCase();

  const run = async (fn: () => Promise<SiteInfo>) => {
    setBusy(true);
    setError(null);
    try {
      onSiteChange(await fn());
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const onBind = () => run(() => api.sites.bindDomain(site.id, domain.trim().toLowerCase()));
  const onCheck = () => run(() => api.sites.checkDomain(site.id));
  const onUnbind = () => {
    setDomain("");
    run(() => api.sites.unbindDomain(site.id));
  };

  return (
    <div className="space-y-5">
      {!site.sites_domain && (
        <div className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
          {t("sites.hostingNotConfigured")}
        </div>
      )}
      <div>
        <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-stone-500">
          {t("sites.platformUrl")}
        </div>
        {site.platform_url ? (
          <CopyableUrl url={site.platform_url} />
        ) : (
          <div className="rounded-lg bg-stone-100 px-3 py-2 text-xs text-stone-400">
            {t("sites.noAddress")}
          </div>
        )}
        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="text-xs text-stone-400">
            {site.status === "active"
              ? t("sites.publishedRev", { rev: site.revision })
              : t("sites.offline")}
          </span>
          <Button variant="outline" size="sm" disabled={publishing} onClick={onRepublish}>
            <IconRefresh size={13} className="mr-1 inline" />
            {publishing ? t("sites.publishing") : t("sites.republish")}
          </Button>
        </div>
      </div>

      <SiteConnectionsPanel site={site} onSiteChange={onSiteChange} />

      {excluded.length > 0 && (
        <div className="rounded-lg bg-stone-50 px-3 py-2 text-xs text-stone-500">
          <div className="mb-1 font-medium text-stone-600">
            {t("sites.excludedFiles", { count: excluded.length })}
          </div>
          <ul className="mono max-h-24 space-y-0.5 overflow-y-auto">
            {excluded.map((f) => (
              <li key={f.path} className={f.sensitive ? "text-red-600" : undefined}>
                {f.path}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-stone-500">
          {t("sites.customDomain")}
        </div>
        <div className="flex items-center gap-2">
          <Input
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            placeholder="www.example.com"
            className="flex-1"
            disabled={!site.sites_domain}
          />
          <Button
            variant="outline"
            disabled={busy || !site.sites_domain || (!bound && !domain.trim())}
            onClick={bound ? onCheck : onBind}
          >
            {bound ? t("sites.checkDns") : t("sites.bindDomain")}
          </Button>
        </div>
        {site.custom_domain && (
          <div className="mt-2 rounded-lg bg-stone-50 px-3 py-2 text-xs text-stone-600">
            <div>{t("sites.cnameInstructions")}</div>
            <div className="mono mt-1">
              CNAME&nbsp;&nbsp;{site.custom_domain}&nbsp;→&nbsp;{site.sites_domain}
            </div>
            <div className="mt-1 text-stone-400">{t("sites.cloudflareHint")}</div>
            <div
              className={`mt-1.5 ${
                site.domain_status === "active" ? "text-manor-700" : "text-amber-600"
              }`}
            >
              {site.domain_status === "active" ? t("sites.dnsActive") : t("sites.dnsPending")}
            </div>
            <button
              type="button"
              className="mt-1 text-xs text-stone-400 underline hover:text-stone-600"
              onClick={onUnbind}
            >
              {t("sites.unbindDomain")}
            </button>
          </div>
        )}
      </div>

      {(error || publishError) && (
        <div className="whitespace-pre-wrap text-xs text-red-600">{error || publishError}</div>
      )}

      {/* Status toggle sits on the same right-hand action column as republish,
          so the drawer has one consistent edge for things you can press. */}
      <div className="flex items-center justify-between gap-2 border-t border-stone-100 pt-3">
        <span className="text-xs text-stone-400">
          {site.status === "active" ? t("sites.liveHint") : t("sites.offlineHint")}
        </span>
        {site.status === "active" ? (
          <Button
            variant="danger"
            size="sm"
            disabled={busy}
            onClick={() => run(() => api.sites.setStatus(site.id, "offline"))}
          >
            <IconEyeOff size={13} className="mr-1 inline" />
            {t("sites.takeOffline")}
          </Button>
        ) : (
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => run(() => api.sites.setStatus(site.id, "active"))}
          >
            <IconEye size={13} className="mr-1 inline" />
            {t("sites.putOnline")}
          </Button>
        )}
      </div>
    </div>
  );
}

export default function SitePublishAction({ doc }: { doc: Document | null }) {
  const docPath = doc?.fs_path || "";
  const [target, setTarget] = useState<string | null>(null);
  const [site, setSite] = useState<SiteInfo | null>(null);
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [excluded, setExcluded] = useState<SitePublishResult["excluded"]>([]);
  const [hostingConfigured, setHostingConfigured] = useState(true);
  const [autoConnectionPlan, setAutoConnectionPlan] =
    useState<SiteAutoConnectionPlan | null>(null);
  // The store's open key is the single source of truth for "my drawer is
  // showing" — tracking it in local state races with the store update and
  // silently strands the drawer with stale content.
  const openKey = useDetailStore((s) => s.payload?.key);
  const drawerKey = site ? `site-${site.id}` : "";
  const drawerOpen = !!drawerKey && openKey === drawerKey;

  const refresh = useCallback(async () => {
    if (!docPath) {
      setChecked(true);
      return null;
    }
    try {
      const r = await api.sites.forPath(docPath);
      setTarget(r.publishable ? r.target : null);
      setSite(r.site);
      setHostingConfigured(r.hosting_configured);
      setAutoConnectionPlan(r.auto_connection_plan);
      return r.site;
    } catch {
      return null;
    } finally {
      setChecked(true);
    }
  }, [docPath]);

  useEffect(() => {
    setChecked(false);
    setSite(null);
    setTarget(null);
    setAutoConnectionPlan(null);
    setExcluded([]);
    refresh();
  }, [refresh]);

  const defaultName = useMemo(() => {
    const base = (target || docPath).split("/").filter(Boolean).pop() || "site";
    return base.replace(/\.[^.]+$/, "");
  }, [target, docPath]);

  const run = async (fn: () => Promise<SiteInfo>) => {
    setBusy(true);
    setError(null);
    try {
      setSite(await fn());
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const publishRef = useRef<() => void>(() => {});

  const buildPayload = (
    s: SiteInfo,
    excludedFiles: SitePublishResult["excluded"],
    publishError: string | null,
  ) => ({
    key: `site-${s.id}`,
    icon: <IconGlobe size={18} />,
    title: t("sites.settings"),
    subtitle: s.name,
    body: (
      <SiteDrawerBody
        site={s}
        excluded={excludedFiles}
        publishError={publishError}
        publishing={busy}
        onRepublish={() => publishRef.current()}
        onSiteChange={setSite}
      />
    ),
    // Only "visit" goes in the footer. Republish and take-offline stay in the
    // body next to what they act on, with their labels visible — the footer's
    // secondary/danger slots render icon-only, and neither action has an icon
    // anyone would recognise without the tooltip.
    primaryAction: {
      label: t("sites.visit"),
      icon: <IconExternalLink size={15} />,
      onClick: () => window.open(s.url, "_blank", "noopener"),
    },
  });

  const publish = async () => {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.sites.publish({
        path: target,
        name: site?.name || defaultName,
        auto_connect: true,
      });
      setExcluded(r.excluded);
      setSite(r);
      openDetail(buildPayload(r, r.excluded, null));
    } catch (e: any) {
      const message = e?.message || String(e);
      setError(message);
      if (site) {
        openDetail(buildPayload(site, excluded, message));
      } else {
        // Nothing published yet, so there is no site drawer to report into.
        openDetail({
          key: `site-error-${target}`,
          icon: <IconGlobe size={18} />,
          title: t("sites.publishFailed"),
          body: <div className="whitespace-pre-wrap text-xs text-red-600">{message}</div>,
          primaryAction: {
            label: t("sites.publish"),
            onClick: () => {
              closeDetail();
              publish();
            },
          },
        });
      }
    } finally {
      setBusy(false);
    }
  };

  publishRef.current = publish;

  const openPublishConfirmation = () => {
    const autoConnect = !!autoConnectionPlan?.eligible;
    openDetail({
      key: `site-publish-${target}`,
      icon: <IconGlobe size={18} />,
      title: t("sites.publishConfirmTitle"),
      subtitle: defaultName,
      body: <PublishConfirmation plan={autoConnectionPlan} />,
      primaryAction: {
        label: autoConnect ? t("sites.confirmPublishAndConnect") : t("sites.confirmPublish"),
        onClick: () => {
          closeDetail();
          publishRef.current();
        },
      },
    });
  };

  // Poll DNS verification while the drawer is open and a domain is pending.
  const siteId = site?.id;
  const domainStatus = site?.domain_status;
  useEffect(() => {
    if (!drawerOpen || !siteId || domainStatus !== "pending") return;
    const timer = setInterval(() => {
      api.sites.checkDomain(siteId).then(setSite).catch(() => undefined);
    }, 15000);
    return () => clearInterval(timer);
  }, [drawerOpen, siteId, domainStatus]);

  // Keep the drawer (body + footer actions) in sync with the current state.
  useEffect(() => {
    if (!drawerOpen || !site) return;
    openDetail(buildPayload(site, excluded, error));
  }, [drawerOpen, site, excluded, busy, error]);

  // Hosting unconfigured and nothing published yet: the whole feature is off on
  // this deployment, so don't advertise a button that can't produce a URL.
  if (!checked || !target || (!hostingConfigured && !site)) return null;

  const published = !!site && site.status === "active";
  const label = published
    ? `${t("sites.published")} · ${site!.url.replace(/^https:\/\//, "")}`
    : site
      ? t("sites.offline")
      : t("sites.publish");

  return (
    <button
      type="button"
      onClick={() => {
        if (site) {
          openDetail(buildPayload(site, excluded, error));
        } else {
          openPublishConfirmation();
        }
      }}
      disabled={busy}
      aria-label={label}
      title={label}
      className={`manor-editor-tool-button manor-editor-icon-button${
        published ? " manor-editor-tool-button--active" : ""
      }`}
      style={{
        opacity: busy ? 0.5 : 1,
        ...(published ? { color: "#2f7268" } : {}),
      }}
    >
      <IconGlobe size={16} />
    </button>
  );
}
