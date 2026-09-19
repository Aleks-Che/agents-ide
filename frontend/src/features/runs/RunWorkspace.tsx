import type { RunRecord } from '../../api/runs'

export function RunWorkspace({ run }: { run?: RunRecord }) {
  const workspace = run?.runtime?.workspace as
    { workspace_path?: string; branch?: string } | undefined
  if (!workspace?.workspace_path) return null
  return (
    <p className="hint run-workspace" aria-label="Рабочий каталог запуска">
      Изолированная папка: <code>{workspace.workspace_path}</code>
      <br />
      Ветка: <code>{workspace.branch}</code>
    </p>
  )
}
