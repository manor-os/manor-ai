import {
  forwardRef,
  type IframeHTMLAttributes,
  type SyntheticEvent,
} from "react";
import { t } from "../../lib/i18n";
import type { IsolatedHtmlPreviewResult } from "../../lib/useIsolatedHtmlPreview";
import LoadingSpinner from "./LoadingSpinner";

interface IsolatedHtmlPreviewFrameProps extends Omit<
  IframeHTMLAttributes<HTMLIFrameElement>,
  "aria-busy" | "data-preview-error" | "sandbox" | "src" | "srcDoc"
> {
  preview: IsolatedHtmlPreviewResult;
  loadingLabel?: string;
  errorLabel?: string;
  retryLabel?: string;
}

const IsolatedHtmlPreviewFrame = forwardRef<
  HTMLIFrameElement,
  IsolatedHtmlPreviewFrameProps
>(function IsolatedHtmlPreviewFrame({
  preview,
  loadingLabel = t("status.loading"),
  errorLabel = t("page.file_viewer.preview_not_available"),
  retryLabel = t("component.chat_message_actions.retry"),
  onLoad,
  ...iframeProps
}, ref) {
  const handleLoad = (event: SyntheticEvent<HTMLIFrameElement>) => {
    // about:blank is only a placeholder while the isolated response is being
    // prepared. Callers should observe loads of actual preview documents only.
    if (preview.previewUrl) onLoad?.(event);
  };

  return (
    <>
      <iframe
        {...iframeProps}
        ref={ref}
        src={preview.previewUrl || "about:blank"}
        referrerPolicy={iframeProps.referrerPolicy || "no-referrer"}
        aria-busy={preview.isPreparingPreview}
        data-preview-error={preview.previewError || undefined}
        onLoad={handleLoad}
      />
      {preview.isPreparingPreview && (
        <div className="isolated-html-preview-status" role="status" aria-live="polite">
          <LoadingSpinner size={18} />
          <span>{loadingLabel}</span>
        </div>
      )}
      {preview.previewError && (
        <div
          className="isolated-html-preview-status is-error"
          role="alert"
          title={preview.previewError}
        >
          <span>{errorLabel}</span>
          <button type="button" onClick={preview.retryPreview}>{retryLabel}</button>
        </div>
      )}
    </>
  );
});

export default IsolatedHtmlPreviewFrame;
