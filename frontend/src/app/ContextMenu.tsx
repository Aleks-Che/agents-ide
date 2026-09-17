import { useEffect, useLayoutEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import type { ContextMenuTarget } from './context_menu'

export function ContextMenu({
  target,
  label,
  items,
  onClose,
}: {
  target: ContextMenuTarget
  label: string
  items: Array<{
    label: string
    onSelect: () => void
    disabled?: boolean
    danger?: boolean
    title?: string
  }>
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const menu = ref.current!
    const rect = menu.getBoundingClientRect()
    menu.style.left = `${Math.max(8, Math.min(target.x, window.innerWidth - rect.width - 8))}px`
    menu.style.top = `${Math.max(8, Math.min(target.y, window.innerHeight - rect.height - 8))}px`
    menu
      .querySelector<HTMLButtonElement>('button:not(:disabled)')
      ?.focus({ preventScroll: true })
  }, [target])

  useEffect(() => {
    const dismissOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !ref.current?.contains(event.target))
        onClose()
    }
    const dismissOnScroll = (event: Event) => {
      if (event.target instanceof Node && ref.current?.contains(event.target))
        return
      onClose()
    }
    document.addEventListener('pointerdown', dismissOutside)
    window.addEventListener('resize', onClose)
    window.addEventListener('scroll', dismissOnScroll, true)
    return () => {
      document.removeEventListener('pointerdown', dismissOutside)
      window.removeEventListener('resize', onClose)
      window.removeEventListener('scroll', dismissOnScroll, true)
    }
  }, [onClose])

  const closeAndFocus = () => {
    target.trigger.focus({ preventScroll: true })
    onClose()
  }

  return createPortal(
    <div
      ref={ref}
      role="menu"
      aria-label={label}
      className="context-menu"
      style={{ left: target.x, top: target.y }}
      onContextMenu={(event) => event.preventDefault()}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) onClose()
      }}
      onKeyDown={(event) => {
        if (event.key === 'Escape' || event.key === 'Tab') {
          event.preventDefault()
          closeAndFocus()
        } else if (
          ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)
        ) {
          event.preventDefault()
          const items = Array.from(
            event.currentTarget.querySelectorAll<HTMLButtonElement>(
              'button:not(:disabled)',
            ),
          )
          const current = items.indexOf(
            document.activeElement as HTMLButtonElement,
          )
          const next =
            event.key === 'Home'
              ? 0
              : event.key === 'End'
                ? items.length - 1
                : (current +
                    (event.key === 'ArrowDown' ? 1 : -1) +
                    items.length) %
                  items.length
          items[next]?.focus()
        }
      }}
    >
      {items.map((item) => (
        <button
          key={item.label}
          type="button"
          role="menuitem"
          className={`quiet${item.danger ? ' danger' : ''}`}
          disabled={item.disabled}
          title={item.title}
          onClick={() => {
            closeAndFocus()
            item.onSelect()
          }}
        >
          {item.label}
        </button>
      ))}
    </div>,
    document.body,
  )
}
