import { useState } from 'react'

import { AGENT_COLORS, AGENT_LABELS, type Evidence } from '../api/client'
import EvidencePanel from './EvidencePanel'

export interface StepView {
  agent_name: string
  step_order: number
  summary: string
  reasoning: string
  input_summary?: string | null
  duration_ms?: number | null
  evidence: Evidence[]
  findings?: Record<string, unknown>
}

interface Props {
  step: StepView
  incidentId: string
  isLast: boolean
  defaultOpen?: boolean
}

function Findings({ findings }: { findings: Record<string, unknown> }) {
  const order = (findings.propagation_order ?? []) as { service: string; onset_offset_s: number }[]
  const stable = (findings.stable_notes ?? []) as string[]
  const spiking = (findings.spiking_services ?? []) as string[]
  const markers = (findings.markers ?? []) as { service: string; event: string; count: number }[]

  const hasAny = order.length || stable.length || spiking.length || markers.length
  if (!hasAny) return null

  return (
    <div className="mb-3 space-y-2">
      {spiking.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] uppercase tracking-wider text-slate-600">Error spikes</p>
          <div className="flex flex-wrap gap-1">
            {spiking.map((service) => (
              <span key={service} className="chip bg-rose-500/10 font-mono text-rose-300">
                {service}
              </span>
            ))}
          </div>
        </div>
      )}

      {markers.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] uppercase tracking-wider text-slate-600">Marker events</p>
          <div className="flex flex-wrap gap-1">
            {markers.slice(0, 6).map((marker) => (
              <span
                key={`${marker.service}-${marker.event}`}
                className="chip bg-slate-800 font-mono text-slate-300"
              >
                {marker.event}
                <span className="text-slate-500">×{marker.count}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {order.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] uppercase tracking-wider text-slate-600">
            Propagation order
          </p>
          <div className="flex flex-wrap items-center gap-1">
            {order.map((entry, index) => (
              <span key={entry.service} className="flex items-center gap-1">
                {index > 0 && <span className="text-slate-700">→</span>}
                <span className="chip bg-slate-800 font-mono text-slate-300">
                  {entry.service}
                  <span className="text-slate-500">+{entry.onset_offset_s}s</span>
                </span>
              </span>
            ))}
          </div>
        </div>
      )}

      {stable.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] uppercase tracking-wider text-slate-600">
            Stayed normal (rules causes out)
          </p>
          <ul className="space-y-0.5">
            {stable.slice(0, 4).map((note) => (
              <li key={note} className="font-mono text-[11px] text-emerald-300/70">
                ✓ {note}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/**
 * One agent's contribution to the reasoning trace.
 *
 * Collapsed it shows the conclusion; expanded it shows the reasoning, the
 * structured findings and every piece of evidence cited.
 */
export default function AgentStep({ step, incidentId, isLast, defaultOpen = false }: Props) {
  const [open, setOpen] = useState(defaultOpen)
  const colors = AGENT_COLORS[step.agent_name] ?? AGENT_COLORS.log_analysis
  const label = AGENT_LABELS[step.agent_name] ?? step.agent_name

  return (
    <li className="relative animate-fade-up pl-10">
      {/* rail + node */}
      {!isLast && <span className="absolute left-[15px] top-8 h-full w-px bg-slate-800" />}
      <span
        className={`absolute left-2 top-2.5 flex h-4 w-4 items-center justify-center rounded-full ring-4 ring-slate-950 ${colors.rail}`}
      >
        <span className="text-[9px] font-bold text-slate-950">{step.step_order}</span>
      </span>

      <div className="card mb-3 overflow-hidden">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="flex w-full items-start gap-3 px-3.5 py-3 text-left transition hover:bg-slate-800/30"
        >
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-2">
              <span className={`text-xs font-semibold uppercase tracking-wider ${colors.text}`}>
                {label}
              </span>
              {step.duration_ms != null && (
                <span className="font-mono text-[10px] text-slate-600">
                  {(step.duration_ms / 1000).toFixed(1)}s
                </span>
              )}
              {step.evidence.length > 0 && (
                <span className="chip bg-slate-800 text-slate-400">
                  {step.evidence.length} evidence
                </span>
              )}
            </span>
            <span className="mt-1.5 block text-sm leading-relaxed text-slate-200">
              {step.summary || <span className="italic text-slate-500">no summary</span>}
            </span>
          </span>
          <span className="mt-1 shrink-0 text-[10px] text-slate-600">{open ? '▲' : '▼'}</span>
        </button>

        {open && (
          <div className="animate-fade-up border-t border-slate-800 px-3.5 py-3">
            {step.input_summary && (
              <p className="mb-3 font-mono text-[11px] text-slate-600">
                input: {step.input_summary}
              </p>
            )}

            {step.reasoning && (
              <div className="mb-3">
                <p className="mb-1 text-[10px] uppercase tracking-wider text-slate-600">Reasoning</p>
                <p className="text-xs leading-relaxed text-slate-300">{step.reasoning}</p>
              </div>
            )}

            {step.findings && <Findings findings={step.findings} />}

            <p className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-600">
              Evidence cited
            </p>
            <EvidencePanel
              incidentId={incidentId}
              evidence={step.evidence}
              emptyLabel="This step cited no evidence."
            />
          </div>
        )}
      </div>
    </li>
  )
}
