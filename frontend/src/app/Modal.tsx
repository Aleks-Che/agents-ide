import { useEffect, useRef, type ReactNode } from 'react'

export function Modal({
  children,
  onClose,
  busy = false,
  labelledBy,
  label,
}: {
  children: ReactNode
  onClose: () => void
  busy?: boolean
  labelledBy?: string
  label?: string
}) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current!
    dialog.showModal()
    return () => dialog.close()
  }, [])
  return (
    <dialog
      ref={ref}
      className="modal-host"
      aria-labelledby={labelledBy}
      aria-label={label}
      aria-busy={busy}
      tabIndex={-1}
      onKeyDown={(event) => {
        if (event.key !== 'Tab') return
        const controls = [
          ...event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]',
          ),
        ].filter((element) => element.getClientRects().length > 0)
        const first = controls[0]
        const last = controls.at(-1)
        if (!first || !last) {
          event.preventDefault()
          event.currentTarget.focus()
        } else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault()
          last.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault()
          first.focus()
        }
      }}
      onCancel={(event) => {
        event.preventDefault()
        if (!busy) onClose()
      }}
    >
      <fieldset className="modal-fields" disabled={busy}>
        {children}
      </fieldset>
    </dialog>
  )
}
