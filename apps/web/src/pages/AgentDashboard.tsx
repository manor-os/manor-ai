import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { relativeTime } from "../lib/format";
import PageHeader from "../components/ui/PageHeader";
import StatusBadge from "../components/ui/StatusBadge";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import { t } from "../lib/i18n";
import { getAgentDescription } from "../lib/localizedContent";

/* -- helpers ------------------------------------------------ */

const FALLBACK_COLORS = [
  { bg: "#e5eeeb", fg: "#436b65" },
  { bg: "#e3e9f1", fg: "#3f57a0" },
  { bg: "#f3e5ed", fg: "#be185d" },
  { bg: "#ece9f5", fg: "#6443a0" },
  { bg: "#f3ecd6", fg: "#936027" },
  { bg: "#dceae3", fg: "#3f7361" },
  { bg: "#e8eff4", fg: "#426c87" },
  { bg: "#f1dddb", fg: "#a23e38" },
];

function getFallbackColor(name: string) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  return FALLBACK_COLORS[Math.abs(hash) % FALLBACK_COLORS.length];
}

/* KPI color themes matching Dashboard */
const KPI_THEMES = [
  { iconBg: "#e3ebe8", iconColor: "#436b65" },
  { iconBg: "#e3e9f1", iconColor: "#4869ac" },
  { iconBg: "#dceae3", iconColor: "#437f6b" },
  { iconBg: "#f3ecd6", iconColor: "#b27c34" },
];

/* -- component --------------------------------------------- */

export default function AgentDashboard() {
  const navigate = useNavigate();

  const { data: agents, isLoading: agentsLoading } = useQuery({
    queryKey: ["agents", "my"],
    queryFn: () => api.agents.list(),
  });

  const { data: executions, isLoading: execLoading } = useQuery({
    queryKey: ["executions", "all"],
    queryFn: () => api.executions.list({ limit: 50 }),
  });

  const { data: subscriptions } = useQuery({
    queryKey: ["agent-subscriptions"],
    queryFn: () => api.agents.subscriptions(),
  });

  const agentList = agents || [];
  const execItems = executions?.items || [];
  const recentExecs = execItems.slice(0, 10);

  const today = new Date().toISOString().slice(0, 10);
  const todayExecs = execItems.filter((e: any) => e.created_at && e.created_at.startsWith(today));
  const activeExecs = execItems.filter((e: any) => e.status === "running");
  const avgResponseMs =
    execItems.length > 0
      ? execItems.reduce((s: number, e: any) => s + (e.duration_ms || 0), 0) / execItems.length
      : 0;

  const execCountByAgent: Record<string, number> = {};
  todayExecs.forEach((e: any) => {
    if (e.agent_id) execCountByAgent[e.agent_id] = (execCountByAgent[e.agent_id] || 0) + 1;
  });

  const latestExecByAgent: Record<string, any> = {};
  execItems.forEach((e: any) => {
    if (e.agent_id && !latestExecByAgent[e.agent_id]) latestExecByAgent[e.agent_id] = e;
  });

  const isLoading = agentsLoading || execLoading;

  if (isLoading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "96px 0" }}>
        <LoadingSpinner size={28} />
      </div>
    );
  }

  const kpiStats = [
    { label: t("page.agent_dashboard.kpi_total_agents"), value: String(agentList.length), icon: "M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" },
    { label: t("page.agent_dashboard.kpi_active_executions"), value: String(activeExecs.length), icon: "M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.348a1.125 1.125 0 010 1.971l-11.54 6.347a1.125 1.125 0 01-1.667-.985V5.653z" },
    { label: t("page.agent_dashboard.kpi_executions_today"), value: String(todayExecs.length), icon: "M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75z" },
    { label: t("page.agent_dashboard.kpi_avg_response"), value: avgResponseMs > 0 ? `${(avgResponseMs / 1000).toFixed(1)}s` : "--", icon: "M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" },
  ];

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <PageHeader
        title={t("nav.agentDashboard")}
        subtitle={`${t("page.agent_dashboard.monitoring")} ${agentList.length} ${agentList.length !== 1 ? t("page.agent_dashboard.agents_plural") : t("page.agent_dashboard.agent_singular")}`}
      >
        <button onClick={() => navigate("/agents")} className="btn-manor-outline" style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 8 }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
          </svg>
          {t("page.agent_dashboard.manage_agents")}
        </button>
      </PageHeader>

      <div style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 24 }}>
        {/* KPI Stats Row - same as Dashboard metrics row */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 150px), 1fr))", gap: 16 }}>
          {kpiStats.map((stat, i) => {
            const theme = KPI_THEMES[i];
            return (
              <div
                key={i}
                className="glass-panel card-hover-surface"
                style={{ padding: 20, cursor: "default" }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                  <div style={{ width: 36, height: 36, borderRadius: 10, background: theme.iconBg, color: theme.iconColor, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
                      <path strokeLinecap="round" strokeLinejoin="round" d={stat.icon} />
                    </svg>
                  </div>
                  <div>
                    <div style={{ fontSize: 12, fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.03em", color: "#78716c", marginBottom: 2 }}>{stat.label}</div>
                    <div style={{ fontSize: 28, fontWeight: 800, color: "#292524", lineHeight: 1 }}>{stat.value}</div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Agent Grid - same card style as My Agents */}
        <div>
          <h2 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", marginBottom: 12 }}>{t("nav.agents")}</h2>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 16 }}>
            {agentList.map((agent) => {
              const color = getFallbackColor(agent.name);
              const latest = latestExecByAgent[agent.id];
              const todayCount = execCountByAgent[agent.id] || 0;
              const isActive = agent.status === "active";
              const description = getAgentDescription(agent);

              return (
                <div
                  key={agent.id}
                  onClick={() => navigate(`/agents/${agent.id}`)}
                  className="glass-card"
                  style={{ cursor: "pointer" }}
                >
                  <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                    <div
                      style={{
                        width: 44,
                        height: 44,
                        borderRadius: 14,
                        background: color.bg,
                        color: color.fg,
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        fontSize: 18,
                        fontWeight: 800,
                        flexShrink: 0,
                        position: "relative",
                      }}
                    >
                      {agent.name[0]?.toUpperCase() || "A"}
                      <span
                        style={{
                          position: "absolute",
                          bottom: -2,
                          right: -2,
                          width: 12,
                          height: 12,
                          borderRadius: "50%",
                          border: "2px solid #fff",
                          background: isActive ? "#44895f" : "#d6d3d1",
                        }}
                      />
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <h3 style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{agent.name}</h3>
                      {description && (
                        <p style={{ fontSize: 12, color: "#a8a29e", margin: "2px 0 0 0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{description}</p>
                      )}
                    </div>
                  </div>

                  <div style={{ marginTop: 12, display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 12 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      {latest ? (
                        <StatusBadge
                          type={latest.status === "completed" ? "success" : latest.status === "failed" ? "danger" : latest.status === "running" ? "info" : "inactive"}
                          dot
                        >
                          {latest.status}
                        </StatusBadge>
                      ) : (
                        <span style={{ color: "#a8a29e" }}>{t("page.agent_dashboard.no_executions")}</span>
                      )}
                    </div>
                    <span style={{ color: "#a8a29e" }}>{todayCount} {t("page.agent_dashboard.today")}</span>
                  </div>
                </div>
              );
            })}

            {agentList.length === 0 && (
              <div style={{ gridColumn: "1 / -1", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "48px 0", textAlign: "center" }}>
                <p style={{ fontSize: 14, color: "#78716c" }}>{t("page.agent_dashboard.no_agents_yet")}</p>
                <button onClick={() => navigate("/agents")} className="btn-manor" style={{ marginTop: 12, fontSize: 13 }}>
                  {t("page.agent_dashboard.create_first_agent")}
                </button>
              </div>
            )}
          </div>
        </div>

        {/* Recent Executions - glass-table */}
        <div>
          <h2 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", marginBottom: 12 }}>{t("page.agent_dashboard.recent_executions")}</h2>
          {recentExecs.length === 0 ? (
            <div className="glass-card" style={{ padding: 32, textAlign: "center" }}>
              <p style={{ fontSize: 13, color: "#a8a29e" }}>{t("page.agent_dashboard.no_executions_recorded")}</p>
            </div>
          ) : (
            <div className="glass-panel" style={{ overflow: "hidden", padding: 0 }}>
              <table className="glass-table">
                <thead>
                  <tr>
                    <th>{t("nav.agents")}</th>
                    <th>{t("page.agent_dashboard.status")}</th>
                    <th>{t("page.agent_dashboard.input")}</th>
                    <th>{t("page.agent_dashboard.duration")}</th>
                    <th>{t("page.agent_dashboard.date")}</th>
                  </tr>
                </thead>
                <tbody>
                  {recentExecs.map((exec: any) => {
                    const execAgent = agentList.find((a) => a.id === exec.agent_id);
                    const agentColor = execAgent ? getFallbackColor(execAgent.name) : null;
                    return (
                      <tr key={exec.id}>
                        <td style={{ padding: "12px 16px" }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                            {agentColor && (
                              <div style={{ width: 24, height: 24, borderRadius: 8, background: agentColor.bg, color: agentColor.fg, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 10, fontWeight: 800, flexShrink: 0 }}>
                                {execAgent!.name[0]?.toUpperCase() || "A"}
                              </div>
                            )}
                            <span style={{ fontSize: 13, color: "#44403c", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              {execAgent?.name || exec.agent_id || "--"}
                            </span>
                          </div>
                        </td>
                        <td style={{ padding: "12px 16px" }}>
                          <StatusBadge
                            type={exec.status === "completed" ? "success" : exec.status === "failed" ? "danger" : exec.status === "running" ? "info" : "inactive"}
                            dot
                          >
                            {exec.status || t("page.browser_sessions.unknown")}
                          </StatusBadge>
                        </td>
                        <td style={{ padding: "12px 16px", color: "#57534e", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 200 }}>
                          {exec.input_preview || exec.input?.substring(0, 60) || "--"}
                        </td>
                        <td style={{ padding: "12px 16px", color: "#57534e" }}>
                          {exec.duration_ms ? `${(exec.duration_ms / 1000).toFixed(1)}s` : "--"}
                        </td>
                        <td style={{ padding: "12px 16px", color: "#a8a29e", fontSize: 12 }}>
                          {relativeTime(exec.created_at)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Subscriptions */}
        {(subscriptions || []).length > 0 && (
          <div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
              <h2 style={{ fontSize: 14, fontWeight: 700, color: "#44403c" }}>{t("page.agent_dashboard.subscribed_agents")}</h2>
              <button onClick={() => navigate("/agents")} style={{ fontSize: 12, fontWeight: 600, color: "#436b65", background: "transparent", border: "none", cursor: "pointer" }}>
                {t("page.agent_dashboard.manage")}
              </button>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12 }}>
              {(subscriptions || []).map((sub: any) => {
                const c = getFallbackColor(sub.name || "A");
                return (
                  <div key={sub.id} className="glass-card" style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <div style={{ width: 36, height: 36, borderRadius: 12, background: c.bg, color: c.fg, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, fontWeight: 800, flexShrink: 0 }}>
                      {(sub.name || "A")[0]?.toUpperCase()}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <p style={{ fontSize: 13, fontWeight: 600, color: "#44403c", margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{sub.name}</p>
                      {sub.category && <span style={{ fontSize: 10, color: "#436b65" }}>{sub.category}</span>}
                    </div>
                    <StatusBadge type="purple">{t("page.agent_dashboard.subscribed")}</StatusBadge>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
