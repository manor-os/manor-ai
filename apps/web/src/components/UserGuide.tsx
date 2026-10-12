import { useState } from "react";
import { t } from "../lib/i18n";
import { useDisplayPreferences, type InterfaceMode } from "../stores/displayPreferences";
import Button from "./ui/Button";
import Modal from "./ui/Modal";
import RadioCard from "./ui/RadioCard";
import "./UserGuide.css";

function InterfacePreview({ mode }: { mode: InterfaceMode }) {
  const professional = mode === "professional";

  return (
    <svg className="user-guide-preview" viewBox="0 0 330 212" aria-hidden="true" focusable="false">
      <rect width="330" height="212" rx="10" className="guide-preview-canvas" />
      <rect width="50" height="212" rx="10" className="guide-preview-sidebar" />
      <path d="M50 0v212" className="guide-preview-divider" />
      <svg x="14" y="16" width="14" height="14" viewBox="0 0 1024 1024" className="guide-preview-ink">
        <path d="M295.152941 0l224.376471 224.376471L743.905882 0H1024v63.247059L519.529412 567.717647 0 49.694118V0h295.152941zM0 256l243.952941 243.952941V1024H0V256z m1024 15.058824v752.941176H780.047059V515.011765L1024 271.058824z" />
      </svg>
      <rect x="12" y="43" width="26" height="19" rx="5" className="guide-preview-control" />
      <path d="M21 52.5h8m-4-4v8" className="guide-preview-icon" />
      {[77, 93, ...(professional ? [109, 125, 141] : [])].map((y) => (
        <g key={y}>
          <rect x="13" y={y} width="5" height="5" rx="1.5" className="guide-preview-ink" />
          <rect x="23" y={y + 1} width="14" height="3" rx="1.5" className="guide-preview-line" />
        </g>
      ))}
      <circle cx="18" cy="195" r="6" className="guide-preview-control" />
      <rect x="29" y="193" width="10" height="3" rx="1.5" className="guide-preview-line" />
      <text x="64" y="24" className="guide-preview-heading">Workspace</text>
      <circle cx="308" cy="20" r="2" className="guide-preview-line" />
      {professional && (
        <g className="guide-preview-tabs">
          <text x="64" y="43" className="guide-preview-label">{t("nav.chat")}</text>
          <text x="110" y="43" className="guide-preview-label">{t("nav.tasks")}</text>
          <text x="158" y="43" className="guide-preview-label">{t("nav.settings")}</text>
          <rect x="64" y="48" width="24" height="2" rx="1" className="guide-preview-ink" />
        </g>
      )}
      <g transform={`translate(0 ${professional ? 16 : 0})`}>
        {[74, 93, 112].map((x, index) => (
          <g key={x} className={`guide-preview-agent guide-preview-agent-${index}`}>
            <circle cx={x} cy="49" r="8" className="guide-preview-control" />
            <circle cx={x} cy="47" r="2" className="guide-preview-person" />
            <path d={`M${x - 3} 53q3-4 6 0`} className="guide-preview-person-stroke" />
          </g>
        ))}
        <rect x="128" y="47" width="36" height="3" rx="1.5" className="guide-preview-line" />
      </g>
      <g className="guide-preview-message">
        <rect x="188" y="83" width="110" height="24" rx="7" className="guide-preview-control" />
        <rect x="199" y="94" width="85" height="3" rx="1.5" className="guide-preview-line" />
      </g>
      {professional && (
        <g className="guide-preview-process">
          <rect x="64" y="113" width="253" height="32" rx="6" className="guide-preview-result-surface" />
          <text x="74" y="125" className="guide-preview-label">{t("component.assistant_message_blocks.processed")}</text>
          <path d="m303 121 3 3 3-3" className="guide-preview-icon" />
          {[74, 151, 228].map((x, index) => (
            <g key={x} className={`guide-preview-trace guide-preview-trace-${index}`}>
              <path d={`m${x} 135 2 2 4-4`} className="guide-preview-icon" />
              <rect x={x + 12} y="134" width="49" height="3" rx="1.5" className="guide-preview-line" />
            </g>
          ))}
        </g>
      )}
      <g className={`guide-preview-reply${professional ? " guide-preview-reply-professional" : ""}`}>
        <rect x="64" y={professional ? 153 : 121} width="211" height="3" rx="1.5" className="guide-preview-line" />
        {!professional && <rect x="64" y="130" width="180" height="3" rx="1.5" className="guide-preview-line" />}
        <g className="guide-preview-result">
          <rect x="64" y={professional ? 165 : 145} width="234" height={professional ? 17 : 30} rx="6" className="guide-preview-result-surface" />
          <path d={professional ? "M75 168h5l3 3v8h-8zm5 0v3h3" : "M75 152h7l4 4v12H75zm7 0v5h4"} className="guide-preview-icon" />
          <rect x="95" y={professional ? 169 : 154} width="102" height="3" rx="1.5" className="guide-preview-ink" />
          <rect x="95" y={professional ? 176 : 162} width="70" height="3" rx="1.5" className="guide-preview-line" />
        </g>
      </g>
      <rect x="64" y="188" width="253" height="16" rx="7" className="guide-preview-control" />
      <path d="M74 196h5m-2.5-2.5v5m226.5.5v-6m-2.5 2.5 2.5-2.5 2.5 2.5" className="guide-preview-icon" />
    </svg>
  );
}

export default function UserGuide() {
  const interfaceMode = useDisplayPreferences((state) => state.interfaceMode);
  const hasSelectedInterfaceMode = useDisplayPreferences((state) => state.hasSelectedInterfaceMode);
  const [selectedMode, setSelectedMode] = useState<InterfaceMode>(interfaceMode);
  const [previewReplay, setPreviewReplay] = useState(0);
  const setInterfaceMode = useDisplayPreferences((state) => state.setInterfaceMode);

  return (
    <Modal
      open
      title={t("page.account.interface_mode")}
      onClose={() => {
        if (!hasSelectedInterfaceMode) setInterfaceMode("simple");
      }}
      maxWidth="760px"
      className="user-guide-dialog"
      footer={(
        <Button onClick={() => {
          setInterfaceMode(selectedMode);
        }}>
          {t("action.save")}
        </Button>
      )}
    >
      <div
        role="radiogroup"
        aria-label={t("page.account.interface_mode")}
        className="user-guide-choices"
        onKeyDown={(event) => {
          if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const nextMode = event.key === "Home" ? "simple" : event.key === "End" ? "professional" : selectedMode === "simple" ? "professional" : "simple";
          setSelectedMode(nextMode);
          setPreviewReplay((replay) => replay + 1);
          event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]')[
            nextMode === "simple" ? 0 : 1
          ]?.focus();
        }}
      >
        <RadioCard
          selected={selectedMode === "simple"}
          onSelect={() => {
            setSelectedMode("simple");
            setPreviewReplay((replay) => replay + 1);
          }}
          tabIndex={selectedMode === "simple" ? 0 : -1}
          ariaDescribedBy="user-guide-simple-description"
          preview={<InterfacePreview key={`simple:${previewReplay}`} mode="simple" />}
          ariaLabel={t("page.account.interface_mode_simple")}
        >
          <span className="flex min-w-0 flex-col gap-1 py-1">
            <span className="flex flex-wrap items-center gap-2 text-sm font-semibold">
              {t("page.account.interface_mode_simple")}
              {!hasSelectedInterfaceMode && (
                <span className="text-xs font-normal text-[var(--text-muted)]">
                  {t("component.user_guide.default")}
                </span>
              )}
            </span>
            <span id="user-guide-simple-description" className="text-xs leading-relaxed text-[var(--text-muted)]">
              {t("component.user_guide.simple_description")}
            </span>
          </span>
        </RadioCard>
        <RadioCard
          selected={selectedMode === "professional"}
          onSelect={() => {
            setSelectedMode("professional");
            setPreviewReplay((replay) => replay + 1);
          }}
          tabIndex={selectedMode === "professional" ? 0 : -1}
          ariaDescribedBy="user-guide-professional-description"
          preview={<InterfacePreview key={`professional:${previewReplay}`} mode="professional" />}
          ariaLabel={t("page.account.interface_mode_professional")}
        >
          <span className="flex min-w-0 flex-col gap-1 py-1">
            <span className="text-sm font-semibold">
              {t("page.account.interface_mode_professional")}
            </span>
            <span id="user-guide-professional-description" className="text-xs leading-relaxed text-[var(--text-muted)]">
              {t("component.user_guide.professional_description")}
            </span>
          </span>
        </RadioCard>
      </div>
    </Modal>
  );
}
