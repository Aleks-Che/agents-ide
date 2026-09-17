import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  describeBindingError,
  templatesApi,
  type PipelineBinding,
} from '../../api/bindings'
import { projectsApi, type Project } from '../../api/projects'
import { Modal } from '../../app/Modal'
import { useCsrfToken } from '../../app/session'

export function AttachTemplateDialog({
  fixedProject,
  initialProjectId,
  initialTemplateId,
  onClose,
  onCreated,
  onEdit,
}: {
  fixedProject?: Project
  initialProjectId?: string | null
  initialTemplateId?: string
  onClose: () => void
  onCreated: (binding: PipelineBinding, project: Project) => void
  onEdit: (templateId: string, project: Project | null) => void
}) {
  const csrf = useCsrfToken()
  const client = useQueryClient()
  const [projectId, setProjectId] = useState(initialProjectId ?? '')
  const [templateId, setTemplateId] = useState(initialTemplateId ?? '')
  const [name, setName] = useState<string | null>(null)
  const projects = useQuery({
    queryKey: ['projects', { includeArchived: false }],
    queryFn: () => projectsApi.list({ includeArchived: false }),
    enabled: !fixedProject,
  })
  const templates = useQuery({
    queryKey: ['templates', { includeArchived: false }],
    queryFn: () => templatesApi.list({ includeArchived: false }),
  })
  const saved = useQuery({
    queryKey: ['saved_template', { templateId }],
    queryFn: () => templatesApi.saved(templateId),
    enabled: Boolean(templateId),
  })
  const project =
    fixedProject ?? projects.data?.find((item) => item.id === projectId)
  const template = templates.data?.find((item) => item.id === templateId)
  const bindingName = name ?? template?.name.slice(0, 120) ?? ''
  const create = useMutation({
    mutationFn: async () => {
      if (
        !project ||
        project.archived ||
        !saved.data ||
        !template ||
        !bindingName.trim()
      )
        throw new Error('Выберите проект и сохранённый шаблон.')
      const binding = await templatesApi.attach(
        template.id,
        {
          project_id: project.id,
          name: bindingName.trim(),
        },
        csrf,
      )
      return { binding, project }
    },
    onSuccess: ({ binding, project: target }) => {
      void client.invalidateQueries({ queryKey: ['bindings'] })
      onCreated(binding, target)
    },
  })
  return (
    <Modal onClose={onClose} busy={create.isPending} label="Создать привязку">
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          if (
            !create.isPending &&
            csrf &&
            project &&
            template &&
            saved.data &&
            bindingName.trim() &&
            !templates.isError &&
            !saved.isError &&
            (fixedProject || !projects.isError)
          )
            create.mutate()
        }}
      >
        <header>
          <h3>Создать привязку</h3>
          <button
            type="button"
            className="quiet"
            aria-label="Закрыть"
            onClick={onClose}
          >
            ×
          </button>
        </header>
        {fixedProject ? (
          <p>
            Проект: <strong>{fixedProject.name}</strong>
          </p>
        ) : (
          <label>
            Проект
            <select
              aria-label="Проект"
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
            >
              <option value="">Выберите проект</option>
              {projects.data
                ?.filter((item) => !item.archived)
                .map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
            </select>
          </label>
        )}
        <label>
          Шаблон
          <select
            aria-label="Шаблон"
            value={templateId}
            onChange={(event) => {
              setTemplateId(event.target.value)
              setName(null)
              create.reset()
            }}
          >
            <option value="">Выберите шаблон</option>
            {templates.data?.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        {(!fixedProject && projects.isLoading) ||
        templates.isLoading ||
        (templateId && saved.isLoading) ? (
          <p role="status">Загружаем шаблоны…</p>
        ) : null}
        {[
          ...(!fixedProject ? [projects] : []),
          templates,
          ...(templateId ? [saved] : []),
        ].map((query, index) =>
          query.error ? (
            <p key={index} role="alert" className="error">
              {describeBindingError(query.error)}{' '}
              <button type="button" onClick={() => void query.refetch()}>
                Повторить загрузку
              </button>
            </p>
          ) : null,
        )}
        {template && saved.isSuccess && !saved.data ? (
          <div className="hint">
            <p>
              Шаблон ещё не сохранён для запуска. Откройте конструктор и нажмите
              «Сохранить».
            </p>
            <button
              type="button"
              onClick={() => onEdit(template.id, project ?? null)}
            >
              Открыть конструктор
            </button>
          </div>
        ) : null}
        {saved.data ? (
          <>
            <label>
              Название в проекте
              <input
                value={bindingName}
                onChange={(event) => setName(event.target.value)}
                maxLength={120}
                required
              />
            </label>
            <p className="hint">
              Привязка будет использовать сохранённый шаблон и получать его
              изменения. Затем откройте диалог проекта и нажмите «Запустить
              шаблон».
            </p>
          </>
        ) : null}
        {create.error ? (
          <p role="alert" className="error">
            {describeBindingError(create.error)}
          </p>
        ) : null}
        <footer>
          <button type="button" className="quiet" onClick={onClose}>
            Отмена
          </button>
          <button
            type="submit"
            disabled={
              !csrf ||
              !project ||
              project.archived ||
              !template ||
              !saved.data ||
              !bindingName.trim() ||
              templates.isError ||
              saved.isError ||
              (!fixedProject && projects.isError)
            }
          >
            {create.isPending ? 'Добавляем…' : 'Создать привязку'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}
