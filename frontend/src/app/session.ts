import { useQueryClient } from '@tanstack/react-query'
import type { Session } from '../api/client'

export function useCsrfToken(): string {
  const client = useQueryClient()
  const session = client.getQueryData<Session>(['session'])
  return session?.csrf_token ?? ''
}
