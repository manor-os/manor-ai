import { Outlet } from "react-router-dom";
import SectionTabs from "./SectionTabs";

/**
 * Minimal section shell — tabs at the top, `<Outlet>` below.
 *
 * Use this when you want URL-bound tabs WITHOUT the full PageHeader
 * chrome (no page title). For the common case, prefer composing:
 *
 *   <PageHeader title="..." tabs={<SectionTabs ... />} />
 *   <Outlet />
 */

interface SectionTab {
  path: string;
  label: string;
  count?: number;
  end?: boolean;
}

interface SectionLayoutProps {
  tabs: SectionTab[];
  children?: React.ReactNode;
}

export default function SectionLayout({ tabs, children }: SectionLayoutProps) {
  return (
    <div className="h-full flex flex-col overflow-hidden">
      <div className="shrink-0 px-4 pt-3 pb-3">
        <SectionTabs tabs={tabs} />
      </div>
      <div className="flex-1 overflow-hidden min-h-0">
        {children ?? <Outlet />}
      </div>
    </div>
  );
}
