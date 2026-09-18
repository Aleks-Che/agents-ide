import { useEffect, useMemo, useState } from 'react'
import {
  NodeHarnessSettings,
  type HarnessExecutionOptions,
} from '../settings/HarnessExecutionSettings'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  type Viewport,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { ApiError } from '../../api/client'
import {
  describeBindingError,
  templatesApi,
  type PipelineTemplate,
  type PipelineVersion,
} from '../../api/bindings'
import {
  graphsApi,
  type Capabilities,
  type GraphDocument,
  type GraphEdge,
  type GraphReport,
  type NodeSchemas,
  type Schema,
} from '../../api/graphs'
import { useCsrfToken } from '../../app/session'
import { Modal } from '../../app/Modal'
import { ModelSelectionEditor } from './ModelSelectionEditor'
import { GitCommitMessageEditor } from './GitCommitMessageEditor'
import { LlmResponseSettings } from './LlmResponseSettings'
import { useEditorResources, type EditorResources } from './resources'
import {
  connectionError,
  contextReferences,
  createNode,
  deleteNode,
  documentFrom,
  downloadJson,
  duplicateNode,
  edgeKey,
  issueLocation,
  nextId,
  object,
  replaceEdge,
  switchExecutor,
} from './graph'
import { DataSchemaEditor, ObjectFields, SchemaField } from './SchemaFields'
import { ExpressionEditor } from './ExpressionEditor'
import { ExecutionReview, GraphTransfer } from './GraphTransfer'
import './editor.css'

type FlowNode = Node<
  { title: string; kind: string; errors: number; warnings: number },
  'pipeline'
>
function PipelineNode({ data }: NodeProps<FlowNode>) {
  return (
    <div className={`pipeline-node ${data.errors ? 'has-error' : ''}`}>
      {data.kind !== 'Start' && (
        <Handle type="target" position={Position.Left} />
      )}
      <small>{data.kind}</small>
      <strong>{data.title}</strong>
      {(data.errors > 0 || data.warnings > 0) && (
        <span className="node-issues">
          Ошибки: {data.errors} · замечания: {data.warnings}
        </span>
      )}
      {data.kind === 'Condition'
        ? ['true', 'false', 'unknown'].map((when, i) => (
            <div key={when}>
              <span className="port-label" style={{ top: `${25 + i * 28}%` }}>
                {when}
              </span>
              <Handle
                id={when}
                type="source"
                position={Position.Right}
                style={{ top: `${25 + i * 28}%` }}
              />
            </div>
          ))
        : data.kind !== 'End' && (
            <Handle type="source" position={Position.Right} />
          )}
    </div>
  )
}
const nodeTypes = { pipeline: PipelineNode }

export function GraphEditor({
  templateId,
  initialVersion,
  onClose,
}: {
  templateId: string
  initialVersion?: PipelineVersion
  onClose: () => void
}) {
  const template = useQuery({
    queryKey: ['graph_editor_template', templateId],
    queryFn: () => templatesApi.get(templateId),
    refetchOnMount: 'always',
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    staleTime: Infinity,
  })
  const schemas = useQuery({
    queryKey: ['node_schemas'],
    queryFn: graphsApi.schemas,
  })
  const capabilities = useQuery({
    queryKey: ['graph_capabilities'],
    queryFn: graphsApi.capabilities,
  })
  const resources = useEditorResources()
  const needsPublishedVersion = Boolean(
    template.data &&
    !initialVersion &&
    !Object.keys(template.data.draft.graph ?? {}).length,
  )
  const versions = useQuery({
    queryKey: ['template_versions', { templateId }],
    queryFn: () => templatesApi.listVersions(templateId),
    enabled: needsPublishedVersion,
    refetchOnMount: 'always',
  })
  const latestVersion = needsPublishedVersion
    ? versions.data?.reduce<PipelineVersion | undefined>(
        (latest, version) =>
          !latest || version.version_number > latest.version_number
            ? version
            : latest,
        undefined,
      )
    : undefined
  const error =
    template.error ??
    schemas.error ??
    capabilities.error ??
    resources.error ??
    versions.error
  if (
    !template.data ||
    !template.isFetchedAfterMount ||
    !schemas.data ||
    !capabilities.data ||
    !resources.data ||
    (needsPublishedVersion && (!versions.data || !versions.isFetchedAfterMount))
  )
    return (
      <Modal onClose={onClose} label="Конструктор графа">
        <div className="dialog">
          {error ? (
            <p role="alert" className="error">
              {describeBindingError(error)}
            </p>
          ) : (
            <p role="status">Загружаем конструктор…</p>
          )}
          <button type="button" onClick={onClose}>
            Закрыть
          </button>
        </div>
      </Modal>
    )
  return (
    <GraphEditorForm
      template={template.data}
      initialVersion={initialVersion ?? latestVersion}
      schemas={schemas.data}
      capabilities={capabilities.data}
      resources={resources.data}
      onClose={onClose}
    />
  )
}

function GraphEditorForm({
  template,
  initialVersion,
  schemas,
  capabilities,
  resources,
  onClose,
}: {
  template: PipelineTemplate
  initialVersion?: PipelineVersion
  schemas: NodeSchemas
  capabilities: Capabilities
  resources: EditorResources
  onClose: () => void
}) {
  const csrf = useCsrfToken(),
    client = useQueryClient()
  const [doc, setDoc] = useState(() =>
    documentFrom(initialVersion ?? template.draft),
  )
  const [saved, setSaved] = useState(() =>
    JSON.stringify(documentFrom(template.draft)),
  )
  const [revision, setRevision] = useState(template.version)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const [tab, setTab] = useState<'node' | 'edge' | 'settings' | 'import'>(
    'node',
  )
  const [reportState, setReportState] = useState<{
    key: string
    report: GraphReport
  } | null>(null)
  const [closeRequested, setCloseRequested] = useState(false)
  const [reloadRequested, setReloadRequested] = useState(false)
  const [localError, setLocalError] = useState('')
  const [notice, setNotice] = useState('')
  const [canvasRevision, setCanvasRevision] = useState(0)
  // React Flow needs measured dimensions on controlled nodes for Fit View,
  // handles and MiniMap. These are editor state, never executable graph fields.
  const [measurements, setMeasurements] = useState<
    Record<string, { width: number; height: number }>
  >({})
  const docKey = JSON.stringify(doc),
    dirty = docKey !== saved
  const report = reportState?.key === docKey ? reportState.report : null
  const selectedNode = doc.graph.nodes.find((n) => n.id === selectedNodeId)
  const editorEdges = doc.graph.edges.map((edge, index) => ({
    ...edge,
    id: edgeKey(edge, index),
  }))
  const selectedEdge = editorEdges.find((e) => e.id === selectedEdgeId)
  const refs = useMemo(() => contextReferences(doc.graph), [doc.graph])
  const immutable = template.kind === 'system' || template.archived
  const operation = useMutation({
    mutationFn: async (action: 'save' | 'validate' | 'export' | 'reload') => {
      setLocalError('')
      setNotice('')
      if (action === 'save') {
        const result = await templatesApi.save(
          template.id,
          { ...doc, expected_version: revision },
          csrf,
        )
        const next = documentFrom(result.draft)
        setDoc(next)
        setSaved(JSON.stringify(next))
        setRevision(result.version)
        setNotice('Шаблон сохранён. Существующие привязки обновлены.')
        await Promise.all([
          client.invalidateQueries({ queryKey: ['templates'] }),
          client.invalidateQueries({ queryKey: ['template_versions'] }),
          client.invalidateQueries({ queryKey: ['saved_template'] }),
          client.invalidateQueries({ queryKey: ['bindings'] }),
        ])
      } else if (action === 'validate') {
        const validation = await graphsApi.validate(doc, csrf)
        setReportState({ key: docKey, report: validation })
        if (validation.ok)
          setNotice(
            'Серверная проверка графа пройдена. Перед запуском выполняется preflight проекта.',
          )
      } else if (action === 'export') {
        downloadJson(await graphsApi.export(doc, csrf), `${template.name}.json`)
      } else if (action === 'reload') {
        const current = await templatesApi.get(template.id)
        const next = documentFrom(current.draft)
        setDoc(next)
        setSaved(JSON.stringify(next))
        setRevision(current.version)
        setReportState(null)
        setSelectedNodeId(null)
        setSelectedEdgeId(null)
        setReloadRequested(false)
        setCanvasRevision((value) => value + 1)
      }
    },
    onError: (error) => {
      if (
        error instanceof ApiError &&
        Array.isArray(error.body.details?.errors)
      ) {
        setReportState({
          key: docKey,
          report: {
            ok: false,
            errors: error.body.details.errors as GraphReport['errors'],
            warnings: [],
            features: [],
          },
        })
      }
    },
  })
  const busy = operation.isPending
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
  const change = (next: GraphDocument) => {
    setDoc(next)
    setLocalError('')
    setNotice('')
  }
  const changeGraph = (graph: GraphDocument['graph']) =>
    change({ ...doc, graph })
  const chooseNode = (id: string) => {
    setSelectedNodeId(id)
    setSelectedEdgeId(null)
    setTab('node')
  }
  const chooseEdge = (id: string) => {
    setSelectedEdgeId(id)
    setSelectedNodeId(null)
    setTab('edge')
  }
  const updateNode = (value: typeof selectedNode) => {
    if (value) {
      const roles = { ...doc.graph.roles }
      const nodes = doc.graph.nodes.map((n) => (n.id === value.id ? value : n))
      nodes.forEach((node) => {
        if (
          typeof node.config?.role === 'string' &&
          ['AgentTask', 'LLMRequest'].includes(node.type)
        )
          roles[node.config.role] ??=
            node.type === 'AgentTask' ? 'agent' : 'llm'
      })
      changeGraph({
        ...doc.graph,
        ...(Object.keys(roles).length ? { roles } : {}),
        nodes,
      })
    }
  }
  const connect = (connection: Connection, existingId?: string) => {
    const source = connection.source,
      target = connection.target
    const when =
      doc.graph.nodes.find((n) => n.id === source)?.type === 'Condition'
        ? (connection.sourceHandle ?? 'true')
        : undefined
    const error = connectionError(doc.graph, source, target, when, existingId)
    if (error) {
      setLocalError(error)
      return
    }
    const existing = editorEdges.find((e) => e.id === existingId)
    const edge: GraphEdge & { id: string } = {
      ...existing,
      id:
        existingId ??
        nextId(
          'edge',
          editorEdges.map((e) => e.id),
        ),
      source,
      target,
    }
    if (when) edge.when = when
    else delete edge.when
    changeGraph({
      ...doc.graph,
      edges: existingId
        ? replaceEdge(doc.graph, edge).edges
        : [...doc.graph.edges, edge],
    })
    chooseEdge(edge.id)
  }
  const nodes: FlowNode[] = doc.graph.nodes.map((n, index) => ({
    id: n.id,
    type: 'pipeline',
    position: n.position ?? {
      x: 60 + (index % 3) * 260,
      y: 60 + Math.floor(index / 3) * 160,
    },
    selected: n.id === selectedNodeId,
    measured: measurements[n.id],
    data: {
      title: n.label ?? n.id,
      kind: n.type,
      errors:
        report?.errors.filter((i) => issueLocation(i, doc.graph).node === n.id)
          .length ?? 0,
      warnings:
        report?.warnings.filter(
          (i) => issueLocation(i, doc.graph).node === n.id,
        ).length ?? 0,
    },
  }))
  const edges: Edge[] = editorEdges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.when,
    selected: e.id === selectedEdgeId,
    label: [e.when, e.label, e.loop ? '↩' : ''].filter(Boolean).join(' · '),
    markerEnd: { type: MarkerType.ArrowClosed },
    type: 'smoothstep',
    style: {
      stroke: report?.errors.some(
        (i) => issueLocation(i, doc.graph).edge === e.id,
      )
        ? '#e05265'
        : e.loop
          ? '#d6a157'
          : '#879bb3',
      strokeWidth: e.id === selectedEdgeId ? 3 : 2,
    },
  }))
  const viewport = object(doc.graph.visual?.viewport)
  const initialViewport: Viewport | undefined = ['x', 'y', 'zoom'].every(
    (k) => typeof viewport[k] === 'number',
  )
    ? (viewport as unknown as Viewport)
    : undefined
  const requestClose = () => {
    if (dirty) setCloseRequested(true)
    else onClose()
  }
  const reportIssues = [...(report?.errors ?? []), ...(report?.warnings ?? [])]
  return (
    <Modal onClose={requestClose} busy={busy} labelledBy="graph-editor-title">
      <div className="graph-editor">
        <header className="editor-header">
          <div>
            <span className="eyebrow">КОНСТРУКТОР</span>
            <h2 id="graph-editor-title">{template.name}</h2>
            <span className="muted">
              {dirty ? 'Есть несохранённые правки' : 'Шаблон сохранён'} ·{' '}
              {doc.origin === 'imported' ? 'импорт' : 'локально'}
            </span>
          </div>
          <div className="editor-actions">
            <button
              type="button"
              className="quiet"
              disabled={!csrf}
              onClick={() => operation.mutate('validate')}
            >
              Проверить граф
            </button>
            <button
              type="button"
              disabled={immutable || !csrf}
              onClick={() => operation.mutate('save')}
            >
              Сохранить
            </button>
            <button
              type="button"
              className="quiet"
              onClick={() => operation.mutate('export')}
            >
              Экспорт JSON
            </button>
            <button type="button" className="quiet" onClick={requestClose}>
              Закрыть конструктор
            </button>
          </div>
        </header>
        {closeRequested && (
          <div role="alert" className="editor-banner">
            Есть несохранённые правки.
            <button type="button" onClick={() => setCloseRequested(false)}>
              Продолжить редактирование
            </button>
            <button type="button" className="danger" onClick={onClose}>
              Закрыть без сохранения
            </button>
          </div>
        )}
        {(localError || operation.error) && (
          <div role="alert" className="editor-banner error">
            {localError || describeBindingError(operation.error)}
            {operation.error instanceof ApiError &&
              operation.error.body.code === 'version_conflict' && (
                <>
                  <p>
                    Шаблон изменён в другом месте. Ваши правки остались в
                    редакторе. Скачайте их перед загрузкой сохранённого шаблона.
                  </p>
                  <button
                    type="button"
                    onClick={() =>
                      downloadJson(doc, `${template.name}-draft.json`)
                    }
                  >
                    Скачать мои правки
                  </button>
                  <button
                    type="button"
                    onClick={() => setReloadRequested(true)}
                  >
                    Загрузить сохранённый шаблон
                  </button>
                </>
              )}
          </div>
        )}
        {reloadRequested && (
          <div className="editor-banner" role="alert">
            Заменить текущие правки сохранённым шаблоном?
            <button type="button" onClick={() => operation.mutate('reload')}>
              Заменить мои правки
            </button>
            <button type="button" onClick={() => setReloadRequested(false)}>
              Отмена
            </button>
          </div>
        )}
        {notice && (
          <p role="status" className="editor-banner">
            {notice}
          </p>
        )}
        <div className="graph-workspace">
          <aside className="graph-palette" aria-label="Палитра узлов">
            <h3>Добавить узел</h3>
            {Object.entries(schemas.nodes).map(([type, schema]) => (
              <button
                type="button"
                className="quiet"
                key={type}
                disabled={
                  immutable ||
                  doc.graph.nodes.length >= schemas.limits.max_nodes ||
                  (type === 'Start' &&
                    doc.graph.nodes.some((n) => n.type === type))
                }
                onClick={() => {
                  const node = createNode(type, schema, doc.graph)
                  changeGraph({
                    ...doc.graph,
                    nodes: [...doc.graph.nodes, node],
                  })
                  chooseNode(node.id)
                }}
              >
                + {type}
              </button>
            ))}
            <h3>Узлы</h3>
            <ul className="graph-node-list">
              {doc.graph.nodes.map((n) => (
                <li key={n.id}>
                  <button
                    type="button"
                    className="quiet"
                    aria-pressed={n.id === selectedNodeId}
                    onClick={() => chooseNode(n.id)}
                  >
                    {n.label ?? n.id}
                  </button>
                </li>
              ))}
            </ul>
            <button
              type="button"
              className="quiet"
              onClick={() => {
                setTab('edge')
                setSelectedEdgeId(null)
              }}
            >
              Добавить связь
            </button>
            <ul className="graph-node-list" aria-label="Связи">
              {editorEdges.map((e) => (
                <li key={e.id}>
                  <button
                    type="button"
                    className="quiet"
                    onClick={() => chooseEdge(e.id)}
                  >
                    {e.source} → {e.target} {e.when ?? ''}
                  </button>
                </li>
              ))}
            </ul>
          </aside>
          <div className="graph-canvas" aria-label="Холст графа">
            <ReactFlow<FlowNode>
              key={canvasRevision}
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              fitView={!initialViewport}
              fitViewOptions={{ maxZoom: 1, padding: 0.15 }}
              defaultViewport={initialViewport}
              minZoom={0.15}
              maxZoom={2}
              nodesDraggable={!immutable}
              nodesConnectable={!immutable}
              edgesReconnectable={!immutable}
              deleteKeyCode={null}
              onNodeClick={(_, node) => chooseNode(node.id)}
              onEdgeClick={(_, edge) => chooseEdge(edge.id)}
              onConnect={connect}
              onReconnect={(edge, connection) => connect(connection, edge.id)}
              onNodesChange={(changes) => {
                const dimensions = changes.filter(
                  (change) => change.type === 'dimensions' && change.dimensions,
                )
                if (dimensions.length)
                  setMeasurements((previous) => {
                    const next = { ...previous }
                    let changed = false
                    for (const change of dimensions) {
                      if (change.type !== 'dimensions' || !change.dimensions)
                        continue
                      if (
                        next[change.id]?.width !== change.dimensions.width ||
                        next[change.id]?.height !== change.dimensions.height
                      ) {
                        next[change.id] = change.dimensions
                        changed = true
                      }
                    }
                    return changed ? next : previous
                  })
                if (immutable) return
                const positions = changes.filter(
                  (c) => c.type === 'position' && c.position,
                )
                if (positions.length)
                  changeGraph({
                    ...doc.graph,
                    nodes: doc.graph.nodes.map((n) => {
                      const p = positions.find(
                        (c) => c.type === 'position' && c.id === n.id,
                      )
                      return p?.type === 'position' && p.position
                        ? { ...n, position: p.position }
                        : n
                    }),
                  })
              }}
              onMoveEnd={(event, next) => {
                if (event && !immutable)
                  changeGraph({
                    ...doc.graph,
                    visual: { ...doc.graph.visual, viewport: next },
                  })
              }}
            >
              <Background />
              <Controls fitViewOptions={{ maxZoom: 1, padding: 0.15 }} />
              <MiniMap pannable zoomable />
            </ReactFlow>
          </div>
          <aside className="graph-inspector">
            <nav className="editor-tabs" aria-label="Панель конструктора">
              {(
                [
                  ['node', 'Узел'],
                  ['edge', 'Связь'],
                  ['settings', 'Входы и роли'],
                  ['import', 'Импорт'],
                ] as const
              ).map(([key, title]) => (
                <button
                  type="button"
                  className="quiet"
                  key={key}
                  aria-pressed={tab === key}
                  onClick={() => setTab(key)}
                >
                  {title}
                </button>
              ))}
            </nav>
            <fieldset className="inspector-fields" disabled={immutable}>
              {tab === 'node' &&
                (selectedNode ? (
                  <>
                    <h3>{selectedNode.id}</h3>
                    {['AgentTask', 'LLMRequest'].includes(
                      selectedNode.type,
                    ) && (
                      <label className="schema-field">
                        Тип исполнителя
                        <select
                          value={selectedNode.type}
                          onChange={(e) =>
                            change(
                              switchExecutor(
                                doc,
                                selectedNode.id,
                                e.target.value as 'AgentTask' | 'LLMRequest',
                              ),
                            )
                          }
                        >
                          <option value="AgentTask">AgentTask</option>
                          <option value="LLMRequest">LLMRequest</option>
                        </select>
                      </label>
                    )}
                    <ObjectFields
                      schema={schemas.nodes[selectedNode.type]}
                      value={selectedNode}
                      label="Параметры узла"
                      references={refs}
                      exclude={[
                        'id',
                        'type',
                        'position',
                        'visual',
                        'config',
                        'expression',
                        'timeout_seconds',
                      ]}
                      onChange={(next) =>
                        updateNode(next as typeof selectedNode)
                      }
                    />
                    {selectedNode.type === 'Condition' && (
                      <ExpressionEditor
                        value={selectedNode.expression ?? { const: true }}
                        references={refs}
                        ast={schemas.ast}
                        onChange={(expression) =>
                          updateNode({ ...selectedNode, expression })
                        }
                      />
                    )}
                    {['AgentTask', 'LLMRequest'].includes(
                      selectedNode.type,
                    ) && (
                      <ModelSelectionEditor
                        kind={
                          selectedNode.type === 'AgentTask' ? 'agent' : 'llm'
                        }
                        value={
                          selectedNode.config?.model_selection ??
                          legacySelection(selectedNode.config ?? {})
                        }
                        resources={resources}
                        inherited={
                          doc.settings.model_selections?.[
                            String(selectedNode.config?.role)
                          ]
                        }
                        onChange={(selection) => {
                          const config = { ...selectedNode.config }
                          for (const key of [
                            'model_selection',
                            'connection_id',
                            'harness_profile_id',
                            'model',
                          ])
                            delete config[key]
                          if (selection) config.model_selection = selection
                          updateNode({ ...selectedNode, config })
                        }}
                      />
                    )}
                    {schemas.nodes[selectedNode.type]?.properties?.config && (
                      <ObjectFields
                        schema={
                          schemas.nodes[selectedNode.type].properties!.config
                        }
                        value={selectedNode.config ?? {}}
                        label="Конфигурация"
                        references={refs}
                        exclude={[
                          'generate_message',
                          'message_generation',
                          'harness_settings',
                          'json_processing',
                          ...(selectedNode.type === 'GitCommit'
                            ? ['verification_node_id']
                            : []),
                          ...(selectedNode.type === 'GitCommit' &&
                          selectedNode.config?.generate_message === true
                            ? ['message']
                            : []),
                          'model_selection',
                          'expected_kind',
                          'connection_id',
                          'harness_profile_id',
                          'model',
                          'output_schema',
                        ]}
                        onChange={(config) =>
                          updateNode({ ...selectedNode, config })
                        }
                      />
                    )}
                    {selectedNode.type === 'AgentTask' ? (
                      <NodeHarnessSettings
                        value={
                          (selectedNode.config?.harness_settings ??
                            {}) as Record<string, HarnessExecutionOptions>
                        }
                        onChange={(harness_settings) => {
                          const config = { ...selectedNode.config }
                          if (Object.keys(harness_settings).length)
                            config.harness_settings = harness_settings
                          else delete config.harness_settings
                          updateNode({ ...selectedNode, config })
                        }}
                      />
                    ) : null}
                    {selectedNode.type === 'GitCommit' ? (
                      <GitCommitMessageEditor
                        key={selectedNode.id}
                        config={selectedNode.config ?? {}}
                        connections={resources.connections}
                        onChange={(config) =>
                          updateNode({ ...selectedNode, config })
                        }
                      />
                    ) : null}
                    {selectedNode.type === 'LLMRequest' &&
                    (selectedNode.config?.response_format === 'json' ||
                      selectedNode.config?.output_schema !== undefined) ? (
                      <LlmResponseSettings
                        config={selectedNode.config ?? {}}
                        onChange={(config) =>
                          updateNode({ ...selectedNode, config })
                        }
                      />
                    ) : null}
                    {['AgentTask', 'LLMRequest'].includes(
                      selectedNode.type,
                    ) && (
                      <>
                        <label className="checkbox-row">
                          <input
                            type="checkbox"
                            checked={
                              selectedNode.config?.output_schema !== undefined
                            }
                            onChange={(e) => {
                              const config = { ...selectedNode.config }
                              if (e.target.checked) {
                                config.output_schema = {
                                  type: 'object',
                                  properties: {},
                                  additionalProperties: false,
                                }
                                config.response_format = 'json'
                              } else delete config.output_schema
                              updateNode({ ...selectedNode, config })
                            }}
                          />
                          Схема структурированного результата
                        </label>
                        {selectedNode.config?.output_schema !== undefined && (
                          <DataSchemaEditor
                            schema={
                              object(
                                selectedNode.config.output_schema,
                              ) as Schema
                            }
                            label="Схема результата"
                            onChange={(output_schema) =>
                              updateNode({
                                ...selectedNode,
                                config: {
                                  ...selectedNode.config,
                                  output_schema,
                                },
                              })
                            }
                          />
                        )}
                      </>
                    )}
                    <div className="editor-actions">
                      <button
                        type="button"
                        className="quiet"
                        disabled={
                          selectedNode.type === 'Start' ||
                          doc.graph.nodes.length >= schemas.limits.max_nodes
                        }
                        onClick={() => {
                          const node = duplicateNode(doc.graph, selectedNode.id)
                          if (node) {
                            changeGraph({
                              ...doc.graph,
                              nodes: [...doc.graph.nodes, node],
                            })
                            chooseNode(node.id)
                          }
                        }}
                      >
                        Дублировать узел
                      </button>
                      <button
                        type="button"
                        className="danger"
                        onClick={() => {
                          changeGraph(deleteNode(doc.graph, selectedNode.id))
                          setSelectedNodeId(null)
                        }}
                      >
                        Удалить узел
                      </button>
                    </div>
                    <p className="hint">
                      При удалении узла его связи удаляются. Ссылки из промптов
                      и условий проверит сервер.
                    </p>
                  </>
                ) : (
                  <p className="panel-empty">
                    Добавьте узел из палитры или выберите его на холсте.
                  </p>
                ))}
              {tab === 'edge' && (
                <EdgeEditor
                  key={
                    selectedEdge
                      ? `${selectedEdge.id}:${selectedEdge.source}:${selectedEdge.target}:${selectedEdge.when ?? ''}`
                      : 'new'
                  }
                  doc={doc}
                  edge={selectedEdge}
                  schemas={schemas}
                  references={refs}
                  onConnect={connect}
                  onChange={(edge) => changeGraph(replaceEdge(doc.graph, edge))}
                  onDelete={() => {
                    changeGraph({
                      ...doc.graph,
                      edges: doc.graph.edges.filter(
                        (e, index) => edgeKey(e, index) !== selectedEdgeId,
                      ),
                    })
                    setSelectedEdgeId(null)
                  }}
                />
              )}
              {tab === 'settings' && (
                <GraphSettings
                  doc={doc}
                  schemas={schemas}
                  capabilities={capabilities}
                  resources={resources}
                  onChange={change}
                />
              )}
              {tab === 'import' && (
                <GraphTransfer
                  resources={resources}
                  onImport={(next) => {
                    change(next)
                    setCanvasRevision((value) => value + 1)
                    setSelectedNodeId(null)
                    setSelectedEdgeId(null)
                    setTab('settings')
                  }}
                />
              )}
            </fieldset>
          </aside>
        </div>
        <footer className="graph-validation">
          <span>
            schema_version {doc.schema_version} · engine{' '}
            {capabilities.engine_version} · features:{' '}
            {(report?.features ?? doc.required_features).join(', ') || '—'} ·
            capability: {capabilities.adapter_capabilities}
          </span>
          {reportIssues.length > 0 && (
            <ul aria-label="Результаты проверки">
              {reportIssues.map((issue, index) => {
                const location = issueLocation(issue, doc.graph)
                return (
                  <li key={index}>
                    <button
                      type="button"
                      className="quiet"
                      onClick={() => {
                        if (location.node) chooseNode(location.node)
                        else if (location.edge) chooseEdge(location.edge)
                        else setTab('settings')
                      }}
                    >
                      {location.node ?? location.edge ?? 'Граф'} · {issue.code}:{' '}
                      {issue.message}
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
          {!report && (
            <span className="muted">
              Текущие правки ещё не проверены сервером.
            </span>
          )}
        </footer>
      </div>
    </Modal>
  )
}

function legacySelection(config: Record<string, unknown>): unknown {
  if (config.model && config.harness_profile_id)
    return {
      kind: 'direct',
      model_id: config.model,
      harness_profile_id: config.harness_profile_id,
    }
  if (config.model && config.connection_id)
    return {
      kind: 'direct',
      model_id: config.model,
      provider_connection_id: config.connection_id,
    }
  return undefined
}

function EdgeEditor({
  doc,
  edge,
  schemas,
  references,
  onConnect,
  onChange,
  onDelete,
}: {
  doc: GraphDocument
  edge?: GraphEdge & { id: string }
  schemas: NodeSchemas
  references: string[]
  onConnect: (connection: Connection, id?: string) => void
  onChange: (edge: GraphEdge & { id: string }) => void
  onDelete: () => void
}) {
  const [source, setSource] = useState(edge?.source ?? ''),
    [target, setTarget] = useState(edge?.target ?? ''),
    [when, setWhen] = useState(edge?.when ?? 'true')
  const condition =
    doc.graph.nodes.find((n) => n.id === source)?.type === 'Condition'
  const error = connectionError(
    doc.graph,
    source,
    target,
    condition ? when : undefined,
    edge?.id,
  )
  const schema = schemas.graph.properties!.edges.items!
  return (
    <section>
      <h3>{edge ? `Связь ${edge.id}` : 'Новая связь'}</h3>
      <label className="schema-field">
        Из узла
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="">Выберите…</option>
          {doc.graph.nodes
            .filter((n) => n.type !== 'End')
            .map((n) => (
              <option key={n.id}>{n.id}</option>
            ))}
        </select>
      </label>
      {condition && (
        <label className="schema-field">
          Выход условия
          <select value={when} onChange={(e) => setWhen(e.target.value)}>
            {['true', 'false', 'unknown'].map((w) => (
              <option key={w}>{w}</option>
            ))}
          </select>
        </label>
      )}
      <label className="schema-field">
        В узел
        <select value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">Выберите…</option>
          {doc.graph.nodes
            .filter((n) => n.type !== 'Start')
            .map((n) => (
              <option key={n.id}>{n.id}</option>
            ))}
        </select>
      </label>
      {error && <p className="hint">{error}</p>}
      <button
        type="button"
        disabled={
          Boolean(error) ||
          (!edge && doc.graph.edges.length >= schemas.limits.max_edges)
        }
        onClick={() =>
          onConnect(
            {
              source,
              target,
              sourceHandle: condition ? when : null,
              targetHandle: null,
            },
            edge?.id,
          )
        }
      >
        {edge ? 'Изменить связь' : 'Создать связь'}
      </button>
      {edge && (
        <>
          <ObjectFields
            schema={schema}
            value={edge}
            label="Параметры связи"
            references={references}
            exclude={[
              'id',
              'edge_id',
              'from',
              'to',
              'source',
              'target',
              'from_node',
              'to_node',
              'when',
              'visual',
              'assignments',
            ]}
            onChange={(next) => onChange(next as GraphEdge & { id: string })}
          />
          <fieldset className="schema-group">
            <legend>Назначения данных</legend>
            {['work.mode', 'work.feedback', 'work.plan_feedback'].map(
              (field) => (
                <div key={field}>
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={edge.assignments?.[field] !== undefined}
                      onChange={(e) => {
                        const assignments = { ...edge.assignments }
                        if (e.target.checked)
                          assignments[field] = {
                            const: field === 'work.mode' ? 'repair' : '',
                          }
                        else delete assignments[field]
                        onChange({ ...edge, assignments })
                      }}
                    />
                    {field}
                  </label>
                  {edge.assignments?.[field] &&
                    (field === 'work.mode' ? (
                      <SchemaField
                        label="Режим после перехода"
                        schema={{ enum: ['initial', 'repair', 'next_item'] }}
                        value={
                          edge.assignments[field].const ??
                          edge.assignments[field].value
                        }
                        onChange={(value) =>
                          onChange({
                            ...edge,
                            assignments: {
                              ...edge.assignments,
                              [field]: { const: value },
                            },
                          })
                        }
                      />
                    ) : (
                      <ExpressionEditor
                        value={edge.assignments[field]}
                        ast={schemas.ast}
                        references={references}
                        label={field}
                        onChange={(value) =>
                          onChange({
                            ...edge,
                            assignments: {
                              ...edge.assignments,
                              [field]: value,
                            },
                          })
                        }
                      />
                    ))}
                </div>
              ),
            )}
          </fieldset>
          <button type="button" className="danger" onClick={onDelete}>
            Удалить связь
          </button>
        </>
      )}
      <p className="hint">
        Обычный узел имеет один выход; Condition — по одному true/false/unknown.
        Обратный переход отмечается параметром loop. Число повторов не
        ограничено.
      </p>
    </section>
  )
}

function GraphSettings({
  doc,
  schemas,
  capabilities,
  resources,
  onChange,
}: {
  doc: GraphDocument
  schemas: NodeSchemas
  capabilities: Capabilities
  resources: EditorResources
  onChange: (doc: GraphDocument) => void
}) {
  const [roleName, setRoleName] = useState('')
  const roles = { ...doc.graph.roles }
  doc.graph.nodes.forEach((n) => {
    if (n.config?.role && ['AgentTask', 'LLMRequest'].includes(n.type))
      roles[String(n.config.role)] ??= n.type === 'AgentTask' ? 'agent' : 'llm'
  })
  return (
    <section>
      <h3>Входы и роли</h3>
      <p>
        schema_version: {doc.schema_version}. Поддержаны:{' '}
        {capabilities.supported_schema.join(', ')}. Capability:{' '}
        {capabilities.adapter_capabilities}.
      </p>
      <SchemaField
        label="Требуемые возможности"
        schema={{
          type: 'array',
          items: { enum: schemas.supported_features },
          maxItems: 64,
        }}
        value={doc.required_features}
        onChange={(value) =>
          onChange({ ...doc, required_features: value as string[] })
        }
      />
      <p className="hint">
        Сервер дополнит возможности по узлам и связям. Возможности модели и
        права проверяются отдельно перед запуском.
      </p>
      <DataSchemaEditor
        schema={
          doc.graph.input_schema ?? {
            type: 'object',
            properties: {},
            additionalProperties: false,
          }
        }
        label="Схема входов"
        onChange={(input_schema) =>
          onChange({ ...doc, graph: { ...doc.graph, input_schema } })
        }
      />
      <ObjectFields
        schema={doc.graph.input_schema ?? { type: 'object' }}
        value={doc.inputs}
        label="Начальные значения входов"
        onChange={(inputs) => onChange({ ...doc, inputs })}
      />
      <h4>Роли шаблона</h4>
      {Object.entries(roles).map(([role, kind]) => (
        <details key={role}>
          <summary>
            {role} · {kind}
          </summary>
          <ModelSelectionEditor
            label={`Роль ${role}`}
            kind={kind}
            resources={resources}
            value={doc.settings.model_selections?.[role]}
            onChange={(value) => {
              const settings = structuredClone(doc.settings),
                selections = { ...settings.model_selections }
              if (value)
                selections[role] = value as NonNullable<
                  typeof settings.model_selections
                >[string]
              else delete selections[role]
              delete settings.model_overrides?.[role]
              delete settings.role_assignments?.[role]
              onChange({
                ...doc,
                graph: { ...doc.graph, roles },
                settings: { ...settings, model_selections: selections },
              })
            }}
          />
          <ObjectFields
            label={`Параметры роли ${role}`}
            schema={{ type: 'object' }}
            value={doc.settings.role_parameters?.[role] ?? {}}
            onChange={(params) =>
              onChange({
                ...doc,
                settings: {
                  ...doc.settings,
                  role_parameters: {
                    ...doc.settings.role_parameters,
                    [role]: params,
                  },
                },
              })
            }
          />
        </details>
      ))}
      <div className="editor-actions">
        <input
          aria-label="Название новой роли"
          value={roleName}
          onChange={(e) => setRoleName(e.target.value)}
        />
        {(['agent', 'llm'] as const).map((kind) => (
          <button
            type="button"
            key={kind}
            className="quiet"
            disabled={
              !/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(roleName) ||
              Object.hasOwn(roles, roleName)
            }
            onClick={() => {
              onChange({
                ...doc,
                graph: { ...doc.graph, roles: { ...roles, [roleName]: kind } },
              })
              setRoleName('')
            }}
          >
            Добавить {kind}
          </button>
        ))}
      </div>
      <p className="hint">
        Выполнение без лимитов времени, вызовов и повторов. Остановить его можно
        кнопкой STOP в диалоге.
      </p>
      <ExecutionReview doc={doc} resources={resources} />
    </section>
  )
}
