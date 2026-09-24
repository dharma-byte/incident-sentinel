import { useCallback, useEffect, useRef, useState } from 'react'

import {
  AGENT_COLORS,
  AGENT_LABELS,
  AGENT_ORDER,
  api,
  streamTriage,
  type Candidate,
  type Evidence,
  type FixStep,
  type Health,
  type IncidentDetail,
  type IncidentSummary,
  type Scenario,
  type StreamedStep,
  type TriageResult,
} from './api/client'
import type { StepView } from './components/AgentStep'
import EvidencePanel from './components/EvidencePanel'
import IncidentHistory from './components/IncidentHistory'
import IncidentTimeline from './components/IncidentTimeline'
import SimulateButton from './components/SimulateButton'
import VerdictCard from './components/VerdictCard'

interface Verdict {
  rootCause: string | null
  confidence: number | null
  candidates: Candidate[]
  fix: { steps?: FixStep[]; prevention?: string }
  model?: string
  cached?: boolean
  durationMs?: number
}

/**
 * Evidence arrives two ways: stored rows from `GET /trace` (with real ids), or
 * citation tokens like "L3" over SSE while a run is in flight. During a live
 * run we render the tokens as placeholders and swap in the stored rows once
 * the trace is refetched, so a streaming step is never empty.
 */
function placeholderEvidence(refs: string[]): Evidence[] {
  return refs.map((ref) => ({
    id: `pending-${ref}`,
    source: ref.startsWith('M') ? 'metric' : 'log',
    service: '—',
    excerpt: `cited ${ref} · loading stored row…`,
    timestamp: new Date().toISOString(),
    source_ref: null,
  }))
}

/** The pipeline, drawn as the hand-off it actually is. */
function PipelineHero() {
  return (
    <div className="card px-6 py-12 text-center sm:px-10">
      <h2 className="text-xl font-semibold tracking-tight text-slate-100">
        Pick a scenario to triage
      </h2>
      <p className="mx-auto mt-2 max-w-lg text-sm leading-relaxed text-slate-400">
        Four specialised agents hand their findings to one another and produce a ranked root
        cause — every claim backed by a stored log line or metric sample you can inspect.
      </p>

      <ol className="mx-auto mt-8 flex max-w-2xl flex-wrap items-center justify-center gap-2">
        {AGENT_ORDER.map((agent, index) => {
          const colors = AGENT_COLORS[agent]
          return (
            <li key={agent} className="flex items-center gap-2">
              {index > 0 && <span className="text-slate-700">→</span>}
              <span
                className={`rounded-lg border border-slate-800 px-3 py-2 text-[11px] font-medium ${colors.bg} ${colors.text}`}
              >
                {AGENT_LABELS[agent]}
              </span>
            </li>
          )
        })}
      </ol>

      <p className="mt-8 text-xs text-slate-600">
        Nothing is mocked: the agents read logs and metrics stored in PostgreSQL.
      </p>
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [incidents, setIncidents] = useState<IncidentSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<IncidentDetail | null>(null)

  const [steps, setSteps] = useState<StepView[]>([])
  const [verdict, setVerdict] = useState<Verdict | null>(null)
  const [running, setRunning] = useState(false)
  const [simulating, setSimulating] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [showEvidence, setShowEvidence] = useState(false)

  const closeStream = useRef<(() => void) | null>(null)

  // -- loading ---------------------------------------------------------- //

  const refreshIncidents = useCallback(async () => {
    try {
      setIncidents(await api.incidents({ limit: 50 }))
    } catch (err) {
      setError((err as Error).message)
    }
  }, [])

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null))
    api.scenarios().then(setScenarios).catch((err: Error) => setError(err.message))
    refreshIncidents()
  }, [refreshIncidents])

  useEffect(() => () => closeStream.current?.(), [])

  const loadTrace = useCallback(async (incidentId: string) => {
    const [trace, incident] = await Promise.all([api.trace(incidentId), api.incident(incidentId)])
    const byId = new Map(trace.evidence.map((item) => [item.id, item]))
    setDetail(incident)
    setSteps(
      trace.steps.map((step) => ({
        agent_name: step.agent_name,
        step_order: step.step_order,
        summary: step.output_summary ?? '',
        reasoning: step.reasoning ?? '',
        input_summary: step.input_summary,
        duration_ms: step.duration_ms,
        evidence: step.evidence_refs
          .map((ref) => byId.get(ref))
          .filter((item): item is Evidence => Boolean(item)),
      })),
    )
    if (trace.root_cause) {
      setVerdict((current) => ({
        rootCause: trace.root_cause,
        confidence: trace.confidence,
        candidates: current?.candidates ?? [],
        fix: current?.fix ?? {},
        model: current?.model,
        cached: current?.cached,
        durationMs: current?.durationMs,
      }))
    }
  }, [])

  const clearSelection = useCallback(() => {
    closeStream.current?.()
    closeStream.current = null
    setRunning(false)
    setSelectedId(null)
    setDetail(null)
    setSteps([])
    setVerdict(null)
    setNotice(null)
    setError(null)
    setShowEvidence(false)
  }, [])

  // Esc returns to the overview, but not mid-run: losing a 90s triage to a
  // stray keypress would be infuriating.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && selectedId && !running) clearSelection()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [selectedId, running, clearSelection])

  const selectIncident = useCallback(
    async (incidentId: string) => {
      closeStream.current?.()
      setRunning(false)
      setError(null)
      setNotice(null)
      setSelectedId(incidentId)
      setSteps([])
      setVerdict(null)
      try {
        await loadTrace(incidentId)
      } catch (err) {
        setError((err as Error).message)
      }
    },
    [loadTrace],
  )

  // -- actions ---------------------------------------------------------- //

  const handleSimulate = async (key: string) => {
    setSimulating(key)
    setError(null)
    try {
      const created = await api.simulate(key)
      await refreshIncidents()
      await selectIncident(created.incident_id)
      setNotice(
        `Generated ${created.log_count.toLocaleString()} log lines and ${created.metric_count.toLocaleString()} metric samples across ${created.services.length} services.`,
      )
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSimulating(null)
    }
  }

  const handleTriage = (refresh = false) => {
    if (!selectedId || running) return
    closeStream.current?.()
    setRunning(true)
    setError(null)
    setNotice(null)
    setSteps([])
    setVerdict(null)

    const incidentId = selectedId
    closeStream.current = streamTriage(
      incidentId,
      {
        onStatus: (payload) => {
          if (payload.state === 'cached') setNotice('Replaying a cached run — no LLM calls spent.')
        },
        onStep: (step: StreamedStep) => {
          setSteps((current) => [
            ...current,
            {
              agent_name: step.agent_name,
              step_order: step.step_order ?? current.length + 1,
              summary: step.summary,
              reasoning: step.reasoning,
              duration_ms: step.duration_ms,
              findings: step.findings,
              evidence: placeholderEvidence(step.evidence_refs ?? []),
            },
          ])
        },
        onComplete: async (result: TriageResult) => {
          setRunning(false)
          setVerdict({
            rootCause: result.root_cause,
            confidence: result.confidence,
            candidates: result.candidates ?? [],
            fix: result.fix ?? {},
            model: result.model,
            cached: result.cached,
            durationMs: result.duration_ms,
          })
          try {
            await loadTrace(incidentId)
          } catch {
            /* the streamed steps are already on screen; keep them */
          }
          refreshIncidents()
        },
        onError: (message) => {
          setRunning(false)
          setError(message)
        },
      },
      { refresh },
    )
  }

  // -- render ----------------------------------------------------------- //

  const uniqueEvidence = Array.from(
    new Map(steps.flatMap((step) => step.evidence).map((item) => [item.id, item])).values(),
  )

  const statusDot = (ok: boolean, warn = false) =>
    `h-1.5 w-1.5 rounded-full ${ok ? 'bg-emerald-400' : warn ? 'bg-amber-400' : 'bg-rose-400'}`

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-3 px-4 py-2.5">
          <div className="flex items-baseline gap-2">
            <button
              type="button"
              onClick={clearSelection}
              className="text-[15px] font-semibold tracking-tight text-slate-100 transition hover:text-sky-400"
            >
              Incident Sentinel
            </button>
            <span className="hidden text-[11px] text-slate-500 sm:inline">
              multi-agent infrastructure triage
            </span>
          </div>

          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            {health ? (
              <>
                <span className="chip bg-slate-800/80 font-mono text-slate-400">
                  <span className={statusDot(health.database === 'up')} />
                  db
                </span>
                <span className="chip bg-slate-800/80 font-mono text-slate-400">
                  <span className={statusDot(health.cache === 'up', true)} />
                  cache
                </span>
                <span className="chip bg-slate-800/80 font-mono text-slate-400">
                  <span className={statusDot(health.llm_available)} />
                  {health.llm_model || health.llm_provider}
                </span>
              </>
            ) : (
              <span className="chip bg-rose-500/15 text-rose-300">backend unreachable</span>
            )}
          </div>
        </div>
      </header>

      <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 px-4 py-4 lg:flex-row">
        {/* sidebar: simulate + history. Fixed width, and min-w-0 so long
            scenario titles truncate instead of widening the column. */}
        <aside className="flex w-full min-w-0 shrink-0 flex-col gap-3 lg:sticky lg:top-[3.9rem] lg:h-[calc(100vh-5rem)] lg:w-[290px]">
          <SimulateButton scenarios={scenarios} busy={simulating} onSimulate={handleSimulate} />
          <IncidentHistory
            incidents={incidents}
            selectedId={selectedId}
            scenarios={scenarios}
            onSelect={selectIncident}
          />
        </aside>

        <main className="min-w-0 flex-1">
          {selectedId ? (
            <>
              <div className="card mb-4 flex flex-wrap items-center gap-3 px-4 py-3">
                <button
                  type="button"
                  onClick={clearSelection}
                  title="Back to the overview (Esc)"
                  className="shrink-0 rounded-lg border border-slate-800 px-2 py-1.5 text-xs text-slate-400 transition hover:border-slate-600 hover:text-slate-200"
                >
                  ←
                </button>
                <div className="min-w-0 flex-1">
                  <p className="truncate font-mono text-xs text-slate-300">
                    {detail?.scenario_type ?? '—'}
                    <span className="text-slate-600"> · seed {detail?.seed ?? '—'}</span>
                  </p>
                  <p className="mt-0.5 truncate font-mono text-[10px] text-slate-600">
                    {detail
                      ? `${detail.log_count.toLocaleString()} logs · ${detail.metric_count.toLocaleString()} metrics · ${detail.evidence_count} cited`
                      : 'loading…'}
                  </p>
                </div>
                <div className="flex shrink-0 gap-2">
                  <button
                    type="button"
                    onClick={() => handleTriage(false)}
                    disabled={running}
                    className="rounded-lg bg-sky-500 px-3.5 py-2 text-xs font-semibold text-slate-950 transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
                  >
                    {running ? 'triaging…' : 'Run triage'}
                  </button>
                  <button
                    type="button"
                    onClick={() => handleTriage(true)}
                    disabled={running}
                    title="Bypass the Redis cache and re-run the agents"
                    className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 transition hover:border-slate-500 disabled:opacity-50"
                  >
                    re-run
                  </button>
                </div>
              </div>

              {notice && (
                <div className="mb-4 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
                  {notice}
                </div>
              )}

              {verdict && (
                <VerdictCard
                  rootCause={verdict.rootCause}
                  confidence={verdict.confidence}
                  candidates={verdict.candidates}
                  fix={verdict.fix}
                  model={verdict.model}
                  cached={verdict.cached}
                  durationMs={verdict.durationMs}
                />
              )}

              <IncidentTimeline
                incidentId={selectedId}
                steps={steps}
                running={running}
                error={error}
              />

              {uniqueEvidence.length > 0 && (
                <section className="card mt-4 p-4">
                  <button
                    type="button"
                    onClick={() => setShowEvidence(!showEvidence)}
                    className="flex w-full items-center justify-between text-left"
                  >
                    <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                      All evidence cited
                    </h2>
                    <span className="text-[11px] text-slate-600">
                      {uniqueEvidence.length} items {showEvidence ? '▲' : '▼'}
                    </span>
                  </button>
                  {showEvidence && (
                    <div className="mt-3 max-h-[28rem] overflow-y-auto pr-1">
                      <EvidencePanel incidentId={selectedId} evidence={uniqueEvidence} />
                    </div>
                  )}
                </section>
              )}
            </>
          ) : (
            <>
              <PipelineHero />
              {error && (
                <p className="mt-3 text-center text-xs text-rose-400">{error}</p>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  )
}
