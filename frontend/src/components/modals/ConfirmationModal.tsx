import { useEffect, useId, useRef } from "react";

interface ConfirmationModalProps {
  isOpen: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmationModal({
  isOpen,
  title,
  message,
  confirmLabel = "Confirmar",
  cancelLabel = "Cancelar",
  onConfirm,
  onCancel,
}: ConfirmationModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (isOpen && !dialog.open) dialog.showModal();
    if (!isOpen && dialog.open) dialog.close();
  }, [isOpen]);

  return (
    <dialog
      aria-labelledby={titleId}
      className="confirmation-modal"
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}
      ref={dialogRef}
    >
      <div className="confirmation-modal__content">
        <h2 id={titleId}>{title}</h2>
        <p>{message}</p>
        <div className="confirmation-modal__actions">
          <button className="button button--secondary" onClick={onCancel} type="button">
            {cancelLabel}
          </button>
          <button className="button button--primary" onClick={onConfirm} type="button">
            {confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}
