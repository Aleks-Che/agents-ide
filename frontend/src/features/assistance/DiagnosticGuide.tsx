import type { AssistanceGuide } from '../../api/assistance'

export function DiagnosticGuide({ guide }: { guide: AssistanceGuide }) {
  return (
    <>
      {!!guide.diagnostic_rules?.length && (
        <details>
          <summary>Как помощник делает выводы</summary>
          <ul>
            {guide.diagnostic_rules.map((rule) => (
              <li key={rule}>{rule}</li>
            ))}
          </ul>
        </details>
      )}
      {guide.diagnostic_cases?.map((item) => (
        <details key={item.id}>
          <summary>{item.title}</summary>
          <p>{item.meaning}</p>
          <b>Порядок проверок</b>
          <ol>
            {item.checks.map((check) => (
              <li key={check}>{check}</li>
            ))}
          </ol>
          <b>Ограничения выводов</b>
          <ul>
            {item.limitations.map((limit) => (
              <li key={limit}>{limit}</li>
            ))}
          </ul>
        </details>
      ))}
    </>
  )
}
