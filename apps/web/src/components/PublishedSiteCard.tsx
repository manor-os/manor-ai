import { externalPreviewFrame } from "../lib/externalPreviewFrame";
import { t } from "../lib/i18n";
import { publishedSiteDisplayUrl, type PublishedSiteAttachment } from "../lib/publishedSites";
import { IconExternalLink, IconGlobe } from "./icons";
import Card from "./ui/Card";
import IconTile from "./ui/IconTile";

/** A website the agent published: its name and address, Open, and a live preview. */
export default function PublishedSiteCard({ site }: { site: PublishedSiteAttachment }) {
  const frame = externalPreviewFrame(site.url);
  return (
    <Card minHeight={0} hoverable={false} borderColor="transparent" className="published-site-card">
      <div className="published-site-card__head">
        <IconTile size={34} title={t("component.published_site.label")}>
          <IconGlobe size={17} />
        </IconTile>
        <div className="published-site-card__meta">
          <span className="published-site-card__name">{site.name}</span>
          <span className="published-site-card__url mono" title={site.url}>
            {publishedSiteDisplayUrl(site.url)}
          </span>
        </div>
        <a
          className="published-site-card__open"
          href={site.url}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={t("component.published_site.open_label", { name: site.name })}
        >
          {t("component.published_site.open")}
          <IconExternalLink size={12} />
        </a>
      </div>
      {frame && (
        <div className="published-site-card__preview">
          <iframe
            key={`${frame.src}#${site.revision ?? ""}`}
            title={t("component.published_site.preview_title", { name: site.name })}
            src={frame.src}
            sandbox={frame.sandbox}
            loading="lazy"
            referrerPolicy="no-referrer"
          />
        </div>
      )}
    </Card>
  );
}

/** Every published site of a reply, above its files. */
export function PublishedSiteCards({ sites }: { sites: PublishedSiteAttachment[] | undefined }) {
  if (!sites?.length) return null;
  return (
    <section className="published-site-cards" aria-label={t("component.published_site.label")}>
      {sites.map((site) => <PublishedSiteCard key={site.siteId} site={site} />)}
    </section>
  );
}
