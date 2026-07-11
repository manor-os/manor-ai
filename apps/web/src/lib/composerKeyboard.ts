export type ComposerKeyboardEvent = {
  key: string;
  isComposing?: boolean;
  keyCode?: number;
  which?: number;
};

const IME_PROCESSING_KEY_CODE = 229;

function isImeProcessing(event: ComposerKeyboardEvent) {
  return (
    event.isComposing === true ||
    event.keyCode === IME_PROCESSING_KEY_CODE ||
    event.which === IME_PROCESSING_KEY_CODE
  );
}

export function shouldHandleComposerEnter(event: ComposerKeyboardEvent) {
  return event.key === "Enter" && !isImeProcessing(event);
}
