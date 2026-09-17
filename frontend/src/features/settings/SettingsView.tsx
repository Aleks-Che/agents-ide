import { useState } from 'react'
import { HarnessProfilesPanel } from './HarnessProfilesPanel'
import { ConnectionsPanel } from './ConnectionsPanel'
import { ModelGroupsPanel } from './ModelGroupsPanel'
import { PlanningCouncilPanel } from './PlanningCouncilPanel'
import { ApplicationLogsPanel } from './ApplicationLogsPanel'
import { GeneralSettingsPanel } from './GeneralSettingsPanel'

const sections = [
  { id: 'general', label: 'Общие', Panel: GeneralSettingsPanel },
  { id: 'harnesses', label: 'Агенты', Panel: HarnessProfilesPanel },
  { id: 'connections', label: 'LLM-подключения', Panel: ConnectionsPanel },
  { id: 'groups', label: 'Группы моделей', Panel: ModelGroupsPanel },
  { id: 'council', label: 'Совет планирования', Panel: PlanningCouncilPanel },
  { id: 'logs', label: 'Журнал', Panel: ApplicationLogsPanel },
] as const

export type SettingsSection = (typeof sections)[number]['id']

export function SettingsView({
  section,
  onSectionChange,
}: {
  section: SettingsSection
  onSectionChange: (section: SettingsSection) => void
}) {
  const [visited, setVisited] = useState<SettingsSection[]>([section])
  function selectSection(next: SettingsSection) {
    setVisited((previous) => [...new Set([...previous, section, next])])
    onSectionChange(next)
  }
  return (
    <section className="main-pane" aria-labelledby="settings-title">
      <header className="main-header">
        <div>
          <span className="eyebrow">НАСТРОЙКИ</span>
          <h2 id="settings-title">Настройки приложения</h2>
          <span className="muted">
            Правки групп и состава совета действуют для новых запусков и планов.
          </span>
        </div>
      </header>
      <div className="settings-layout">
        <nav className="settings-menu" aria-label="Разделы настроек">
          {sections.map(({ id, label }) => (
            <button
              type="button"
              key={id}
              className="quiet"
              aria-pressed={section === id}
              aria-controls={`settings-section-${id}`}
              onClick={() => selectSection(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="settings-content">
          {sections.map(({ id, Panel }) => (
            <div key={id} id={`settings-section-${id}`} hidden={section !== id}>
              {visited.includes(id) || section === id ? <Panel /> : null}
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
