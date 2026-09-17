import type { HTMLAttributes } from 'react'

export interface ContextMenuTarget {
  x: number
  y: number
  trigger: HTMLElement
}

export function contextMenuHandlers(
  open: (target: ContextMenuTarget) => void,
): Pick<
  HTMLAttributes<HTMLElement>,
  'onContextMenu' | 'onKeyDown' | 'aria-haspopup'
> {
  return {
    'aria-haspopup': 'menu',
    onContextMenu(event) {
      event.preventDefault()
      open({ x: event.clientX, y: event.clientY, trigger: event.currentTarget })
    },
    onKeyDown(event) {
      if (
        event.key !== 'ContextMenu' &&
        !(event.shiftKey && event.key === 'F10')
      )
        return
      event.preventDefault()
      const rect = event.currentTarget.getBoundingClientRect()
      open({ x: rect.left, y: rect.bottom, trigger: event.currentTarget })
    },
  }
}
