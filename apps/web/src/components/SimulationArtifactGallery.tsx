import { useState } from "react";
import { getLocale } from "../lib/i18n";
import type { SimulationArtifact } from "./WorkspaceSimulationRuntime";
import {
  IconArchive,
  IconAudioWave,
  IconDocument,
  IconGrid4,
  IconImage,
  IconLayers,
  IconPlay,
} from "./icons";
import Button from "./ui/Button";
import Modal from "./ui/Modal";
import StatusBadge from "./ui/StatusBadge";
import "./SimulationArtifactGallery.css";

type ArtifactWithReceipt = SimulationArtifact & {
  status?: string;
  simulated?: boolean;
  receipt_id?: string;
};

function ArtifactIcon({ kind, size = 20 }: { kind: SimulationArtifact["kind"]; size?: number }) {
  if (kind === "video") return <IconPlay size={size} />;
  if (kind === "image") return <IconImage size={size} />;
  if (kind === "audio") return <IconAudioWave size={size} />;
  if (kind === "spreadsheet") return <IconGrid4 size={size} />;
  if (kind === "presentation") return <IconLayers size={size} />;
  if (kind === "archive") return <IconArchive size={size} />;
  return <IconDocument size={size} />;
}

function durationLabel(seconds?: number) {
  if (!seconds) return null;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return minutes ? `${minutes}:${remainder.toString().padStart(2, "0")}` : `${remainder}s`;
}

function ArtifactPreview({ artifact }: { artifact: ArtifactWithReceipt }) {
  if (artifact.kind === "video" && artifact.preview_url) {
    return (
      <video controls playsInline preload="metadata" src={artifact.preview_url}>
        <track kind="captions" />
      </video>
    );
  }
  if (artifact.kind === "audio" && artifact.preview_url) {
    return <audio controls preload="metadata" src={artifact.preview_url} />;
  }
  if (artifact.kind === "image" && artifact.preview_url) {
    return <img src={artifact.preview_url} alt="" />;
  }
  return (
    <div className={`simulation-artifact-preview-placeholder simulation-artifact-preview-placeholder--${artifact.kind}`}>
      <ArtifactIcon kind={artifact.kind} size={34} />
      <strong>{artifact.filename}</strong>
      <span>{artifact.mime_type}</span>
    </div>
  );
}

export default function SimulationArtifactGallery({
  artifacts,
}: {
  artifacts: ArtifactWithReceipt[];
}) {
  const [selected, setSelected] = useState<ArtifactWithReceipt | null>(null);
  const zh = getLocale().toLowerCase().startsWith("zh");
  if (!artifacts.length) return null;

  return (
    <>
      <section className="simulation-artifact-gallery" aria-label={zh ? "模拟生成产物" : "Simulated generated outputs"}>
        <div className="simulation-artifact-gallery__header">
          <div>
            <strong>{zh ? "Blueprint 生成产物" : "Blueprint outputs"}</strong>
            <span>{zh ? "可预览，但不会下载、发布或写入外部系统" : "Previewable; never downloaded, published, or written externally"}</span>
          </div>
          <StatusBadge type="neutral">Simulation</StatusBadge>
        </div>
        <div className="simulation-artifact-gallery__grid">
          {artifacts.map((artifact) => (
            <button
              className={`simulation-artifact-card simulation-artifact-card--${artifact.kind}`}
              key={artifact.id}
              onClick={() => setSelected(artifact)}
              type="button"
            >
              <span className="simulation-artifact-card__preview" aria-hidden="true">
                {artifact.kind === "image" && artifact.preview_url ? (
                  <img src={artifact.preview_url} alt="" loading="lazy" />
                ) : (
                  <span className="simulation-artifact-card__icon">
                    <ArtifactIcon kind={artifact.kind} size={22} />
                    {artifact.kind === "video" && <span className="simulation-artifact-card__play"><IconPlay size={12} /></span>}
                  </span>
                )}
              </span>
              <span className="simulation-artifact-card__copy">
                <strong>{artifact.title}</strong>
                <span>{artifact.filename}</span>
                <small>
                  {artifact.stage || "output"}
                  {durationLabel(artifact.duration_seconds) ? ` · ${durationLabel(artifact.duration_seconds)}` : ""}
                </small>
              </span>
            </button>
          ))}
        </div>
      </section>

      <Modal
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title={selected?.title || (zh ? "模拟产物" : "Simulated output")}
        maxWidth="760px"
        footer={<Button variant="outline" onClick={() => setSelected(null)}>{zh ? "关闭" : "Close"}</Button>}
      >
        {selected && (
          <div className="simulation-artifact-modal">
            <div className="simulation-artifact-modal__notice">
              <StatusBadge type="neutral">Simulation</StatusBadge>
              <span>{zh ? "这是 Blueprint 提供的体验预览，不是正式产物。" : "This is a Blueprint experience preview, not a live artifact."}</span>
            </div>
            <div className="simulation-artifact-modal__preview">
              <ArtifactPreview artifact={selected} />
            </div>
            <dl className="simulation-artifact-modal__meta">
              <div><dt>{zh ? "文件" : "File"}</dt><dd>{selected.filename}</dd></div>
              <div><dt>{zh ? "类型" : "Type"}</dt><dd>{selected.mime_type}</dd></div>
              {selected.receipt_id && <div><dt>{zh ? "模拟回执" : "Simulation receipt"}</dt><dd>{selected.receipt_id}</dd></div>}
            </dl>
            {selected.summary && <p>{selected.summary}</p>}
          </div>
        )}
      </Modal>
    </>
  );
}
