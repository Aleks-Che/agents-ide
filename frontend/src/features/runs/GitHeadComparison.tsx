import type { GitChangesReview } from '../../api/runs'

export function GitHeadComparison({
  head,
}: {
  head: NonNullable<GitChangesReview['head']>
}) {
  const relations = {
    same: 'HEAD совпадает',
    fast_forward: 'Новые коммиты поверх ожидаемого HEAD',
    rewound: 'Откат истории',
    diverged: 'История разошлась',
    unavailable: 'История недоступна',
  }
  const risks = { medium: 'Средний', high: 'Высокий', unknown: 'Не установлен' }
  return (
    <section aria-label="Сравнение HEAD">
      <p>
        <code>{head.expected}</code> → <code>{head.current}</code>
      </p>
      <p>
        <strong>{relations[head.relation]}</strong>
      </p>
      <p>{head.assessment}</p>
      <h5>Новые коммиты</h5>
      <ul>
        {head.commits.map((commit) => (
          <li key={commit.sha}>
            <code>{commit.sha.slice(0, 8)}</code> {commit.subject}
          </li>
        ))}
      </ul>
      {head.omitted_commits > 0 && (
        <p>Не показано коммитов: {head.omitted_commits}</p>
      )}
      <h5>Итоговые изменения между коммитами</h5>
      {!head.changes.length && <p>Содержимое деревьев коммитов совпадает.</p>}
      {head.changes.map((file) => (
        <article key={file.path}>
          <strong>{file.path}</strong>
          <p>
            {file.status === 'D'
              ? 'Удалён'
              : file.status === 'A'
                ? 'Добавлен'
                : 'Изменён'}{' '}
            · Риск: {risks[file.risk]}
          </p>
          <p>{file.assessment}</p>
          {file.diff !== null && file.diff !== undefined && (
            <details>
              <summary>Разница между коммитами</summary>
              <pre>{file.diff}</pre>
              {file.diff_truncated && (
                <p>Показан фрагмент. Для решения изучите полную разницу.</p>
              )}
            </details>
          )}
        </article>
      ))}
      {head.omitted_changes > 0 && (
        <p>Не показано файлов: {head.omitted_changes}</p>
      )}
      <h5>Незакоммиченная работа — сохраняется отдельно</h5>
      {head.worktree_changes.length ? (
        <ul>
          {head.worktree_changes.map((file) => (
            <li key={file.path}>
              <code>{file.code}</code> {file.path}
            </li>
          ))}
        </ul>
      ) : (
        <p>Незакоммиченных изменений нет.</p>
      )}
      {head.omitted_worktree_changes > 0 && (
        <p>Не показано локальных изменений: {head.omitted_worktree_changes}</p>
      )}
    </section>
  )
}
