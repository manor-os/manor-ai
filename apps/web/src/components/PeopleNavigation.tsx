import { useLocation } from "react-router-dom";
import { t } from "../lib/i18n";
import { CLIENTS_PATH } from "../lib/peopleNavigation";
import { FEATURE_DISCOVERIES } from "../lib/featureDiscovery";
import { useAuthStore } from "../stores/auth";
import useFeatureDiscovery from "./useFeatureDiscovery";
import SectionTabs from "./ui/SectionTabs";
import { FeatureDiscoveryBadge } from "./ui/FeatureDiscoveryHint";

/** Shared section identity; Staff and clients retain their own data and permissions. */
export default function PeopleNavigation() {
  const location = useLocation();
  const userId = useAuthStore((s) => s.user?.id);
  const clientsDiscovery = useFeatureDiscovery({
    feature: FEATURE_DISCOVERIES.clients,
    userId,
    alreadyUsed: false,
    viewed: location.pathname === CLIENTS_PATH,
    autoShow: false,
  });
  const tabs = [
    {
      path: location.pathname === "/team/roles" ? "/team/roles" : "/team",
      label: t("page.people.internal_team"),
      end: true,
    },
    {
      path: CLIENTS_PATH,
      label: t("page.people.clients"),
      end: true,
      badge: clientsDiscovery.badgeVisible
        ? <FeatureDiscoveryBadge label={t("component.feature_discovery.new")} />
        : undefined,
    },
  ];

  return <SectionTabs tabs={tabs} ariaLabel={t("nav.people")} />;
}
