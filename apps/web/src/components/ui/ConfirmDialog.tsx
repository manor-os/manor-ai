import Modal from "./Modal";
import Button from "./Button";

interface ConfirmDialogProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  loading?: boolean;
  closeOnConfirm?: boolean;
  error?: string;
  restoreFocusFallback?: () => void;
}

export default function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
  loading = false,
  closeOnConfirm = true,
  error,
  restoreFocusFallback,
}: ConfirmDialogProps) {
  const closeIfIdle = () => {
    if (!loading) onClose();
  };

  return (
    <Modal
      open={open}
      onClose={closeIfIdle}
      title={title}
      restoreFocusFallback={restoreFocusFallback}
      footer={
        <>
          <Button variant="outline" onClick={closeIfIdle} disabled={loading}>
            {cancelLabel}
          </Button>
          <Button
            variant={danger ? "danger" : "primary"}
            loading={loading}
            onClick={() => {
              onConfirm();
              if (closeOnConfirm) onClose();
            }}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <p className="confirm-dialog-message">{message}</p>
      {error && (
        <p className="confirm-dialog-error" role="alert">
          {error}
        </p>
      )}
    </Modal>
  );
}
