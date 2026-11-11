import { useState, type ReactNode } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { t } from "../../lib/i18n";
import PageHeader from "../../components/ui/PageHeader";
import SmartToolbar from "../../components/ui/SmartToolbar";
import TabSwitcher from "../../components/ui/TabSwitcher";

/**
 * Internal team section in Team & Clients.
 * Staff composition and member/role controls remain scoped to the team.
 *
 * Subpages (TeamPeople, TeamRoles) render only their own toolbar and
 * body — no duplicate titles.
 */
export default function TeamLayout() {
  const [teamSearch, setTeamSearch] = useState("");
  const [teamControls, setTeamControls] = useState<{
    toolbar?: ReactNode;
    actions?: ReactNode;
  }>({});
  const location = useLocation();
  const navigate = useNavigate();
  const currentTab = location.pathname === "/team/roles" ? "roles" : "people";

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <PageHeader
        title={t("nav.team")}
        // The page's main switch sits on the title row, like every Settings page.
        tabs={(
          <TabSwitcher
            ariaLabel={t("page.people.internal_team")}
            tabs={[
              { key: "people", label: t("page.team_layout.people") },
              { key: "roles", label: t("page.team_layout.roles") },
            ]}
            value={currentTab}
            onChange={(k) => navigate(k === "roles" ? "/team/roles" : "/team")}
          />
        )}
        toolbar={currentTab === "people" ? (
          <SmartToolbar
            searchValue={teamSearch}
            onSearchChange={setTeamSearch}
            searchPlaceholder={t("page.team_layout.search_team")}
            className="w-full sm:w-64"
          />
        ) : undefined}
        actions={teamControls.actions}
      />

      {teamControls.toolbar && (
        <div className="mb-4 flex min-w-0 shrink-0 flex-wrap items-center gap-3">
          {teamControls.toolbar}
        </div>
      )}

      {/* Subpage body */}
      <div className="flex-1 overflow-hidden min-h-0">
        <Outlet context={{ teamSearch, setTeamControls }} />
      </div>
    </div>
  );
}
