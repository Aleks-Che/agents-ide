import { createContext, useContext } from 'react'
import type { AssistanceTarget } from '../../api/assistance'

export const AssistanceActions = createContext<{
  open: (target?: AssistanceTarget) => void
}>({ open: () => undefined })

export function useAssistance() {
  return useContext(AssistanceActions)
}
