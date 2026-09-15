import { HarnessProfilesPanel } from './HarnessProfilesPanel'
import { ConnectionsPanel } from './ConnectionsPanel'
import { ModelGroupsPanel } from './ModelGroupsPanel'

export function SettingsView() {
  return (
    <section className="main-pane" aria-labelledby="settings-title">
      <header className="main-header">
        <div>
          <span className="eyebrow">НАСТРОЙКИ</span>
          <h2 id="settings-title">Профили, подключения и группы</h2>
          <span className="muted">
            Правки действуют только на новые Run. Существующие снимки Run
            сохраняют свои настройки.
          </span>
        </div>
      </header>
      <div className="settings-grid">
        <HarnessProfilesPanel />
        <ConnectionsPanel />
        <ModelGroupsPanel />
      </div>
    </section>
  )
}
