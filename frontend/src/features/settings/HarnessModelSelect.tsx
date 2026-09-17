import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  describeError,
  harnessApi,
  type HarnessProfile,
} from '../../api/settings'
import { useCsrfToken } from '../../app/session'

import { defaultHarnessModel } from './harness_queries'

export function HarnessModelSelect({
  harness,
  value,
  onChange,
  onValidityChange,
  id,
}: {
  harness?: HarnessProfile
  value: string
  onChange: (value: string) => void
  onValidityChange?: (valid: boolean) => void
  id?: string
}) {
  const csrf = useCsrfToken()
  const client = useQueryClient()
  const catalog = useQuery({
    queryKey: ['harness_catalog', harness?.id],
    queryFn: async () => {
      const catalog = await harnessApi.refreshCatalog(harness!.id, csrf)
      const updated = await harnessApi.get(harness!.id)
      client.setQueryData<HarnessProfile[]>(
        ['installed_harnesses'],
        (previous) =>
          previous?.map((item) => (item.id === updated.id ? updated : item)),
      )
      return catalog
    },
    enabled: Boolean(harness && csrf),
    staleTime: 30_000,
    retry: false,
  })
  const models = catalog.data?.models ?? []
  const defaultModel = defaultHarnessModel(harness)
  const valid = Boolean(
    harness &&
    !catalog.error &&
    catalog.data?.status === 'fresh' &&
    models.some((m) => m.id === value),
  )
  useEffect(() => {
    if (
      !value &&
      defaultModel &&
      catalog.data?.models.some((m) => m.id === defaultModel)
    ) {
      onChange(defaultModel)
    }
  }, [value, defaultModel, catalog.data, onChange])
  useEffect(() => {
    onValidityChange?.(valid)
  }, [valid, onValidityChange])
  return (
    <div className="harness-model-select">
      <select
        id={id}
        aria-label="Модель harness"
        required
        value={value}
        disabled={!harness || catalog.isFetching}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">
          {catalog.isFetching ? 'Загружаем модели…' : 'Выберите модель'}
        </option>
        {value && !models.some((m) => m.id === value) ? (
          <option value={value} disabled>
            Недоступна · {value}
          </option>
        ) : null}
        {models.map((model) => (
          <option key={model.id} value={model.id}>
            {model.id}
            {model.id === defaultModel ? ' · по умолчанию' : ''}
          </option>
        ))}
      </select>
      {catalog.error ? (
        <p className="error" role="alert">
          {describeError(catalog.error)}{' '}
          <button
            type="button"
            className="quiet"
            onClick={() => void catalog.refetch()}
          >
            Повторить
          </button>
        </p>
      ) : harness && catalog.isSuccess && models.length === 0 ? (
        <p className="hint">
          Нет доступных моделей. Настройте провайдера и вход в самой harness.
        </p>
      ) : null}
    </div>
  )
}
