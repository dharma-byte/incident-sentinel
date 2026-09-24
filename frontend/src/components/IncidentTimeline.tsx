import { AGENT_LABELS, AGENT_ORDER } from '../api/client'
import AgentStep, { type StepView } from './AgentStep'

interface Props {
  incidentId: string
  steps: StepView[]
  running: boolean
  currentAgent?: string | null
  error?: string | null
}

/** The agent that should be shown as "thinking" while the stream is open. */
function pendingAgent(steps: StepView[]): string | null {
  const done = new Set(steps.map((step) => step.agent_name))
  return AGENT_ORDER.find((name) => !done.has(name)) ?? null
}

function PendingStep({ agent, index }: { agent: string; index: number }) {
  return (
    <li className="relative pl-10">
      <span className="absolute left-2 top-2.5 flex h-4 w-4 animate-pulse-ring items-center justify-center rounded-full bg-slate-700">
        <span className="text-[9px] font-bold text-slate-950">{index}</span>
      </span>
      <div className="card mb-3 border-dashed px-3.5 py-3">
        <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
          {AGENT_LABELS[agent] ?? agent}
        </span>
        <p className="mt-1.5 flex items-center gap-2 text-sm text-slate-500">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-600 border-t-transparent" />
          reasoning…
        </p>
      </div>
    </li>
  )
}

/**
 * The reasoning trace as a vertical timeline.
 *
 * The point of the whole UI: each agent's step appears the moment it
 * completes, so a reviewer watches the reasoning unfold instead of being
 * handed a verdict.
 */
export default function IncidentTimeline({ incidentId, steps, running, error }: Props) {
  const pending = running ? pendingAgent(steps) : null

  if (steps.length === 0 && !running && !error) {
    return (
      <div className="card flex flex-col items-center justify-center px-6 py-14 text-center">
        <p className="text-sm text-slate-400">No reasoning trace yet.</p>
        <p className="mt-1 max-w-sm text-xs leading-relaxed text-slate-600">
          Run triage on this incident and the four agents will hand off to one another here, each
          showing the evidence behind its conclusion.
        </p>
      </div>
    )
  }

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400">
          Reasoning trace
        </h2>
        <span className="font-mono text-[11px] text-slate-600">
          {steps.length}/{AGENT_ORDER.length} agents
        </span>
      </div>

      {error && (
        <div className="mb-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          {error}
        </div>
      )}

      <ol className="relative">
        {steps.map((step, index) => (
          <AgentStep
            key={`${step.agent_name}-${step.step_order}`}
            step={step}
            incidentId={incidentId}
            isLast={index === steps.length - 1 && !pending}
            defaultOpen={step.agent_name === 'root_cause'}
          />
        ))}
        {pending && <PendingStep agent={pending} index={steps.length + 1} />}
      </ol>
    </div>
  )
}
