import { Minus, Plus, Repeat2 } from 'lucide-react'
import type { RunObservation } from '../../api/runs'

export function LoopProgress({
  observation,
  disabled,
  onAdjust,
}: {
  observation?: RunObservation | null
  disabled: boolean
  onAdjust: (key: string, delta: number) => void
}) {
  if (!observation?.loops?.length) return null
  return (
    <section className="run-loops" aria-label="Циклы выполнения">
      <h4>
        <Repeat2 size={15} aria-hidden="true" /> Циклы
      </h4>
      <div className="run-loop-list">
        {observation.loops.map((loop) => {
          const routes = observation.edges
            ?.filter((edge) => loop.edge_ids.includes(edge.id))
            .map((edge) =>
              [edge.source, edge.target]
                .map(
                  (id) =>
                    observation.nodes?.find((node) => node.id === id)?.label ??
                    id,
                )
                .join(' → '),
            )
          return (
            <article
              key={loop.key}
              className={`run-loop${loop.waiting ? ' exhausted' : ''}`}
              aria-label={`Цикл ${loop.id}`}
            >
              <div className="run-loop-title">
                <strong>{loop.id}</strong>
                <small>{[...new Set(routes)].join(' · ')}</small>
                {loop.scope !== 'run' ? (
                  <small>
                    Пункт: {loop.scope} · всего повторов: {loop.total_completed}
                  </small>
                ) : null}
              </div>
              <div className="run-loop-counts" aria-live="polite">
                <span>
                  Пройдено <strong>{loop.completed}</strong>
                </span>
                <span>
                  Осталось <strong>{loop.remaining}</strong>
                </span>
              </div>
              <div className="run-loop-buttons">
                <button
                  type="button"
                  className="quiet"
                  aria-label={`Уменьшить остаток цикла ${loop.id}`}
                  title="Убрать один следующий повтор"
                  disabled={
                    disabled || !loop.can_adjust || loop.remaining === 0
                  }
                  onClick={() => onAdjust(loop.key, -1)}
                >
                  <Minus size={14} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="quiet"
                  aria-label={`Увеличить остаток цикла ${loop.id}`}
                  title="Добавить один следующий повтор"
                  disabled={disabled || !loop.can_adjust}
                  onClick={() => onAdjust(loop.key, 1)}
                >
                  <Plus size={14} aria-hidden="true" />
                </button>
              </div>
              <progress
                value={Math.min(loop.completed, loop.max_iterations)}
                max={Math.max(1, loop.max_iterations)}
                aria-label={`Прогресс цикла ${loop.id}`}
              />
              {loop.waiting ? (
                <p>
                  {loop.remaining === 0
                    ? 'Повторы закончились. Добавьте итерации и нажмите «Продолжить выполнение».'
                    : 'Итерации добавлены. Нажмите «Продолжить выполнение».'}
                </p>
              ) : null}
            </article>
          )
        })}
      </div>
      <small>
        Счётчик растёт при переходе по loop. Цикл может завершиться раньше по
        условию; изменение остатка не прерывает текущий этап.
      </small>
    </section>
  )
}
