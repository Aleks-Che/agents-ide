import { object } from './graph'

export function LlmResponseSettings({
  config,
  onChange,
}: {
  config: Record<string, unknown>
  onChange: (config: Record<string, unknown>) => void
}) {
  const processing = object(config.json_processing)
  const options = [
    ['strip_thinking_tags', 'Удалять блоки размышлений перед JSON'],
    ['extract_json', 'Извлекать JSON из Markdown и окружающего текста'],
  ] as const
  return (
    <fieldset className="schema-group">
      <legend>Обработка JSON-ответа</legend>
      {options.map(([key, label]) => (
        <label className="checkbox-row" key={key}>
          <input
            type="checkbox"
            checked={processing[key] === true}
            onChange={(event) => {
              const next = { ...processing }
              if (event.target.checked) next[key] = true
              else delete next[key]
              const updated = { ...config }
              if (Object.keys(next).length) updated.json_processing = next
              else delete updated.json_processing
              onChange(updated)
            }}
          />
          {label}
        </label>
      ))}
      <p className="muted">
        Удаление размышлений поддерживает теги think, thinking, analysis и
        reasoning. Извлечение принимает один целый JSON-объект. Исходный ответ
        сохраняется, а извлечённый результат проверяется по схеме узла.
      </p>
    </fieldset>
  )
}
