// onBack is optional. When supplied, a back arrow renders in the header's
// top-left and the title shifts right to make room. Used by the SO modal's
// Related Records tab, where clicking a related record swaps the modal's
// contents in place and the operator needs a way back to where they
// started. Omitting it renders exactly what every other caller renders.
export default function Modal({ title, onClose, children, footer, size, onBack, backLabel }) {
  const className = size ? `modal modal-${size}` : 'modal';
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className={className} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          {onBack && (
            <button
              type="button"
              className="modal-back"
              onClick={onBack}
              aria-label={backLabel ? `Back to ${backLabel}` : 'Back'}
              title={backLabel ? `Back to ${backLabel}` : 'Back'}
              data-testid="modal-back"
            >
              &#8592;
            </button>
          )}
          <h2>{title}</h2>
          <button className="modal-close" onClick={onClose}>&times;</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  );
}
