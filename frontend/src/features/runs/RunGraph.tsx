import { useMemo } from 'react'
import {
  Background,
  Controls,
  MarkerType,
  Position,
  ReactFlow,
  type Node,
  type Edge,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { RunObservation } from '../../api/runs'
import { selectedEdge } from './observation'

export default function RunGraph({
  observation,
  selected,
  onSelect,
}: {
  observation: RunObservation
  selected: string
  onSelect: (id: string) => void
}) {
  const nodes = useMemo<Node[]>(
    () =>
      (observation.nodes ?? []).map((node, index) => ({
        id: node.id,
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        position:
          node.position &&
          typeof node.position.x === 'number' &&
          typeof node.position.y === 'number'
            ? { x: node.position.x, y: node.position.y }
            : { x: (index % 3) * 250, y: Math.floor(index / 3) * 130 },
        data: {
          label: (
            <>
              <strong>{node.label}</strong>
              <br />
              {node.type} · {node.status}
              <br />
              цикл {node.cycle_id ?? '—'} · попыток {node.attempt_count}
            </>
          ),
        },
        selected: selected === node.id,
        className: `observed-node ${observation.current_node_id === node.id ? 'current' : ''} status-${node.status}`,
      })),
    [observation, selected],
  )
  const edges = useMemo<Edge[]>(
    () =>
      (observation.edges ?? []).map((edge) => {
        const loop = observation.loops?.find((item) =>
          item.edge_ids.includes(edge.id),
        )
        return {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          markerEnd: { type: MarkerType.ArrowClosed },
          label: [
            edge.when,
            loop
              ? `цикл ${loop.id}: пройдено ${loop.completed}, осталось ${loop.remaining}`
              : edge.loop_id
                ? `цикл ${edge.loop_id}`
                : '',
          ]
            .filter(Boolean)
            .join(' · '),
          className: selectedEdge(edge, observation.last_transition)
            ? 'observed-edge-selected'
            : '',
          style: selectedEdge(edge, observation.last_transition)
            ? { stroke: '#dcca86', strokeWidth: 4 }
            : undefined,
        }
      }),
    [observation],
  )
  return (
    <div className="run-graph" aria-label="Граф выполнения">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodesDraggable={false}
        nodesConnectable={false}
        onNodeClick={(_, node) => onSelect(node.id)}
        fitView
        minZoom={0.1}
        maxZoom={2}
        colorMode="dark"
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
