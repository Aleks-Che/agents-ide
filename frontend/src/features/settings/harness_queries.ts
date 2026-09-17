import { useQuery } from '@tanstack/react-query'
import { harnessApi, type HarnessProfile } from '../../api/settings'
import { useCsrfToken } from '../../app/session'

export function useInstalledHarnesses(enabled = true) {
  const csrf = useCsrfToken()
  return useQuery({
    queryKey: ['installed_harnesses'],
    queryFn: () => harnessApi.discover(csrf),
    enabled: enabled && Boolean(csrf),
    staleTime: 30_000,
  })
}

export function defaultHarnessModel(harness?: HarnessProfile): string {
  return typeof harness?.settings.default_model === 'string'
    ? harness.settings.default_model
    : ''
}
