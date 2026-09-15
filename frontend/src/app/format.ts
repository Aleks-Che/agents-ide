export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('ru-RU', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function shortHash(
  value: string | null | undefined,
  length = 7,
): string {
  if (!value) return '—'
  return value.length > length ? value.slice(0, length) : value
}

export function basename(value: string): string {
  if (!value) return ''
  const normalised = value.replace(/[\\/]+$/, '')
  const parts = normalised.split(/[\\/]/)
  return parts[parts.length - 1] ?? normalised
}

export function pluraliseRuns(count: number): string {
  const mod10 = count % 10
  const mod100 = count % 100
  if (mod10 === 1 && mod100 !== 11) return `${count} процесс`
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20))
    return `${count} процесса`
  return `${count} процессов`
}
