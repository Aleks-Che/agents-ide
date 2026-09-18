import type { PipelineBinding } from '../../api/bindings'

export function WorkspacePolicyFields({
  workspaceMode,
  branchPolicy,
  onWorkspaceMode,
  onBranchPolicy,
}: {
  workspaceMode: NonNullable<PipelineBinding['workspace_mode']>
  branchPolicy: PipelineBinding['branch_policy']
  onWorkspaceMode: (
    value: NonNullable<PipelineBinding['workspace_mode']>,
  ) => void
  onBranchPolicy: (value: PipelineBinding['branch_policy']) => void
}) {
  const isolated = workspaceMode === 'worktree'
  return (
    <>
      <label>
        Рабочий каталог
        <select
          aria-label="Рабочий каталог"
          value={workspaceMode}
          onChange={(event) => {
            const mode = event.target.value as typeof workspaceMode
            onWorkspaceMode(mode)
            if (mode === 'worktree') onBranchPolicy('run_branch')
          }}
        >
          <option value="project">Папка проекта</option>
          <option value="worktree">Изолированная папка (git worktree)</option>
        </select>
      </label>
      <label>
        Ветка
        <select
          aria-label="Ветка"
          value={branchPolicy}
          disabled={isolated}
          onChange={(event) =>
            onBranchPolicy(event.target.value as typeof branchPolicy)
          }
        >
          <option value="run_branch">Отдельная ветка запуска</option>
          <option value="current">Текущая ветка</option>
        </select>
      </label>
      {isolated ? (
        <p className="hint">
          Для каждого запуска создаётся отдельная папка и ветка из коммита на
          момент старта. Незакоммиченные и игнорируемые файлы не копируются.
          Папка проекта остаётся на своей ветке. Worktree сохраняется после
          завершения; при продолжении используется та же папка.
        </p>
      ) : null}
    </>
  )
}
