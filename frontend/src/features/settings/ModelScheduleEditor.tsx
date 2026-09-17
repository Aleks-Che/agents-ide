import { useId, useState } from 'react'
import type { ModelGroupMember } from '../../api/settings'

export type ModelSchedule = NonNullable<ModelGroupMember['schedule']>

const weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
const hours = Array.from({ length: 24 }, (_, hour) => hour)
const hourLabel = (hour: number) => `${String(hour).padStart(2, '0')}:00`

function initialSchedule(): ModelSchedule {
  return {
    enabled: true,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    same_every_day: false,
    days: weekdays.map(() => hours.map(() => true)),
  }
}

export function ModelScheduleEditor({
  value,
  onChange,
}: {
  value: ModelSchedule | null
  onChange: (value: ModelSchedule) => void
}) {
  const id = useId()
  const [paintAllowed, setPaintAllowed] = useState(false)
  const zones = Array.from(
    new Set([
      'UTC',
      value?.timezone ?? 'UTC',
      ...Intl.supportedValuesOf('timeZone'),
    ]),
  ).sort()

  function paint(day: number, hour: number) {
    if (!value || value.days[day][hour] === paintAllowed) return
    onChange({
      ...value,
      days: value.days.map((row, index) =>
        index === day
          ? row.map((allowed, h) => (h === hour ? paintAllowed : allowed))
          : row,
      ),
    })
  }

  return (
    <div className="model-schedule">
      <label className="enabled-toggle">
        <input
          type="checkbox"
          checked={value?.enabled ?? false}
          aria-controls={`${id}-schedule`}
          onChange={(event) =>
            onChange({
              ...(value ?? initialSchedule()),
              enabled: event.target.checked,
            })
          }
        />
        Использовать расписание
      </label>
      {value?.enabled ? (
        <div id={`${id}-schedule`} className="schedule-editor">
          <label className="enabled-toggle">
            <input
              type="checkbox"
              checked={value.same_every_day}
              onChange={(event) => {
                const same = event.target.checked
                onChange({
                  ...value,
                  same_every_day: same,
                  days: Array.from({ length: same ? 1 : 7 }, () => [
                    ...value.days[0],
                  ]),
                })
              }}
            />
            Одно расписание для всех дней
          </label>
          <p className="hint">
            При объединении расписание понедельника применяется ко всем дням.
          </p>
          <label className="schedule-timezone" htmlFor={`${id}-timezone`}>
            Часовой пояс
            <select
              id={`${id}-timezone`}
              value={value.timezone}
              onChange={(event) =>
                onChange({ ...value, timezone: event.target.value })
              }
            >
              {zones.map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </select>
          </label>
          <div
            className="schedule-tools"
            role="group"
            aria-label="Цвет для часов"
          >
            <button
              type="button"
              className="quiet"
              aria-pressed={paintAllowed}
              onClick={() => setPaintAllowed(true)}
            >
              <span className="schedule-swatch allowed" /> Можно использовать
            </button>
            <button
              type="button"
              className="quiet"
              aria-pressed={!paintAllowed}
              onClick={() => setPaintAllowed(false)}
            >
              <span className="schedule-swatch" /> Пропускать
            </button>
          </div>
          <p className="hint" id={`${id}-help`}>
            Выберите цвет и нажимайте на часы или проводите мышью с зажатой
            кнопкой. Каждая ячейка — один час. В запрещённое время используется
            следующая модель в группе.
          </p>
          <div className="schedule-scroll">
            <table
              className="schedule-grid"
              aria-label="Разрешённые часы использования модели"
              aria-describedby={`${id}-help`}
            >
              <thead>
                <tr>
                  <th scope="col">День</th>
                  {hours.map((hour) => (
                    <th key={hour} scope="col">
                      {String(hour).padStart(2, '0')}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {value.days.map((row, day) => {
                  const label = value.same_every_day ? 'Все дни' : weekdays[day]
                  return (
                    <tr key={day}>
                      <th scope="row">{label}</th>
                      {row.map((allowed, hour) => {
                        const interval = `${label}, ${hourLabel(hour)}–${hourLabel(hour + 1)}`
                        return (
                          <td key={hour}>
                            <button
                              type="button"
                              className={`schedule-hour${allowed ? ' allowed' : ''}`}
                              aria-label={interval}
                              aria-pressed={allowed}
                              title={`${interval}: ${allowed ? 'можно использовать' : 'пропускать'}`}
                              onPointerDown={(event) => {
                                if (event.button === 0) paint(day, hour)
                              }}
                              onPointerEnter={(event) => {
                                if (event.buttons === 1) paint(day, hour)
                              }}
                              onClick={() => paint(day, hour)}
                            >
                              {allowed ? '✓' : '·'}
                            </button>
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <div className="schedule-tools">
            <button
              type="button"
              className="quiet"
              onClick={() =>
                onChange({
                  ...value,
                  days: value.days.map(() => hours.map(() => true)),
                })
              }
            >
              Разрешить все часы
            </button>
            <button
              type="button"
              className="quiet"
              onClick={() =>
                onChange({
                  ...value,
                  days: value.days.map(() => hours.map(() => false)),
                })
              }
            >
              Запретить все часы
            </button>
          </div>
          {!value.days.some((day) => day.some(Boolean)) ? (
            <p className="hint" role="status">
              Все часы запрещены: модель будет всегда пропускаться.
            </p>
          ) : null}
          <p className="hint">
            Расписание проверяется перед каждым вызовом. Уже начатый вызов
            продолжит работу.
          </p>
        </div>
      ) : null}
    </div>
  )
}
