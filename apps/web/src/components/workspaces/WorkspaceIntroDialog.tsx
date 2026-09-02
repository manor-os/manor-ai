import { useEffect, useId, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { t } from "../../lib/i18n";
import { IconArrowRight } from "../icons";
import AiEditButton from "../ui/AiEditButton";
import Button from "../ui/Button";
import Modal from "../ui/Modal";
import "./WorkspaceIntroDialog.css";

const WORKSPACE_EXPLAINER_VIDEO_URL = "/assets/workspace/workspace-intro-original.mp4";

function currentReducedMotionPreference() {
  return typeof window !== "undefined"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export default function WorkspaceIntroDialog() {
  const navigate = useNavigate();
  const explanationId = useId();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [open, setOpen] = useState(false);
  const [reduceMotion, setReduceMotion] = useState(currentReducedMotionPreference);

  useEffect(() => {
    if (typeof window === "undefined") return undefined;

    const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const syncReducedMotion = () => {
      const shouldReduceMotion = motionPreference.matches;
      setReduceMotion(shouldReduceMotion);
      if (shouldReduceMotion) videoRef.current?.pause();
    };
    syncReducedMotion();
    motionPreference.addEventListener("change", syncReducedMotion);

    return () => {
      motionPreference.removeEventListener("change", syncReducedMotion);
    };
  }, []);

  const closeDialog = () => setOpen(false);
  const createWorkspace = () => {
    closeDialog();
    navigate("/workspaces/new");
  };

  return (
    <>
      <AiEditButton
        className="workspace-intro-launcher"
        onClick={() => setOpen(true)}
        title={t("component.workspace_intro.create_workspace")}
        aria-label={t("component.workspace_intro.create_workspace")}
        aria-expanded={open}
        label={t("component.workspace_intro.create_workspace")}
      />

      <Modal
        open={open}
        onClose={closeDialog}
        title={t("component.workspace_intro.dialog_title")}
        className="workspace-intro-dialog"
        bodyClassName="workspace-intro-dialog-body"
        maxWidth="560px"
        footer={
          <Button variant="primary" size="sm" onClick={createWorkspace}>
            {t("component.workspace_intro.create_workspace")}
            <IconArrowRight size={14} />
          </Button>
        }
      >
        <div className="workspace-intro-dialog-video-frame">
          <video
            ref={videoRef}
            className="workspace-intro-dialog-video"
            autoPlay={!reduceMotion}
            muted
            loop={!reduceMotion}
            playsInline
            controls
            preload="metadata"
            aria-label={t("component.workspace_intro.video_label")}
            aria-describedby={explanationId}
          >
            <source src={WORKSPACE_EXPLAINER_VIDEO_URL} type="video/mp4" />
            {t("component.workspace_intro.video_fallback")}
          </video>
        </div>
        <div id={explanationId} className="workspace-intro-dialog-copy">
          <h3>{t("component.workspace_intro.headline")}</h3>
          <p>{t("component.workspace_intro.summary")}</p>
        </div>
      </Modal>
    </>
  );
}
