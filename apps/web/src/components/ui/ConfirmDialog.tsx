import Modal from "./Modal";

interface ConfirmDialogProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
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
}: ConfirmDialogProps) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <button className="btn-manor-outline" onClick={onClose}>
            {cancelLabel}
          </button>
          <button
            className={danger ? "btn-manor-danger" : "btn-manor"}
            onClick={() => { onConfirm(); onClose(); }}
            style={danger ? { background: "#c14a44", color: "#fff" } : undefined}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      <p style={{ color: "#57534e", fontSize: "14px", lineHeight: 1.6 }}>{message}</p>
    </Modal>
  );
}
