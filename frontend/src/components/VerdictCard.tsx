import type { Candidate, FixStep } from '../api/client'

interface Props {
  rootCause: string | null
  confidence: number | null
  candidates: Candidate[]
  fix: { steps?: FixStep[]; prevention?: string }
  model?: string
  cached?: boolean
  durationMs?: number
}

const KIND_STYLES: Record<string, string> = {
  mitigate: 'bg-rose-500/15 text-rose-300',
  fix: 'bg-sky-500/15 text-sky-300',
  verify: 'bg-emerald-500/15 text-emerald-300',
}

function ConfidenceBar({ value, highlight }: { value: number; highlight: boolean }) {
  const pct = Math.round(value * 100)
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 shrink-0 overflow-hidden rounded-full bg-slate-800">
        <div
          className={`h-full rounded-full transition-all duration-700 ${
            highlight ? 'bg-agent-cause' : 'bg-slate-600'
          }`}
          style={{ width: `${Math.max(pct, 2)}%` }}
        />
      </div>
      <span
        className={`w-9 shrink-0 text-right font-mono text-[11px] ${
          highlight ? 'text-agent-cause' : 'text-slate-500'
        }`}
      >
        {pct}%
      </span>
    </div>
  )
}

/**
 * The final verdict: ranked causes with confidence bars, then the fix.
 *
 * Alternatives are shown rather than hidden — a diagnosis you can argue with
 * is worth more than one asserted flatly.
 */
export default function VerdictCard({
  rootCause,
  confidence,
  candidates,
  fix,
  model,
  cached,
  durationMs,
}: Props) {
  if (!rootCause) return null
  const steps = fix.steps ?? []

  return (
    <section className="card animate-fade-up mb-6 border-agent-cause/30 p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-agent-cause">
          Diagnosis
        </h2>
        {cached && (
          <span className="chip bg-emerald-500/15 text-emerald-300" title="Served from Redis">
            cached
          </span>
        )}
        {model && <span className="font-mono text-[10px] text-slate-600">{model}</span>}
        {durationMs != null && (
          <span className="font-mono text-[10px] text-slate-600">
            {(durationMs / 1000).toFixed(1)}s
          </span>
        )}
      </div>

      <p className="mb-4 text-[15px] leading-relaxed text-slate-100">{rootCause}</p>

      {candidates.length > 0 && (
        <div className="mb-4">
          <p className="mb-2 text-[10px] uppercase tracking-wider text-slate-600">
            Ranked candidates
          </p>
          <ul className="space-y-2">
            {candidates.map((candidate, index) => (
              <li
                key={`${candidate.service}-${index}`}
                className={`rounded-lg border px-3 py-2 ${
                  index === 0
                    ? 'border-agent-cause/30 bg-agent-cause/5'
                    : 'border-slate-800 bg-slate-950/40'
                }`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-mono text-[11px] text-slate-400">{candidate.service}</span>
                  <ConfidenceBar value={candidate.confidence} highlight={index === 0} />
                </div>
                <p className="mt-1 text-xs leading-relaxed text-slate-300">{candidate.cause}</p>
                {candidate.why && (
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-500">{candidate.why}</p>
                )}
                {candidate.evidence_refs.length > 0 && (
                  <p className="mt-1 font-mono text-[10px] text-slate-600">
                    cites {candidate.evidence_refs.join(', ')}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {steps.length > 0 && (
        <div>
          <p className="mb-2 text-[10px] uppercase tracking-wider text-slate-600">
            Suggested remediation
          </p>
          <ol className="space-y-2">
            {steps.map((step) => (
              <li key={step.order} className="flex gap-2.5">
                <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-slate-800 font-mono text-[10px] text-slate-400">
                  {step.order}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`chip ${KIND_STYLES[step.kind] ?? 'bg-slate-800 text-slate-300'}`}>
                      {step.kind}
                    </span>
                    <span className="text-xs text-slate-200">{step.action}</span>
                  </div>
                  {step.rationale && (
                    <p className="mt-0.5 text-[11px] leading-relaxed text-slate-500">
                      {step.rationale}
                    </p>
                  )}
                  {step.risk && step.risk.toLowerCase() !== 'low' && (
                    <p className="mt-0.5 text-[11px] text-amber-400/80">risk: {step.risk}</p>
                  )}
                </div>
              </li>
            ))}
          </ol>
          {fix.prevention && (
            <p className="mt-3 border-t border-slate-800 pt-2 text-[11px] leading-relaxed text-slate-500">
              <span className="text-slate-600">prevention · </span>
              {fix.prevention}
            </p>
          )}
        </div>
      )}

      {confidence != null && candidates.length === 0 && (
        <ConfidenceBar value={confidence} highlight />
      )}
    </section>
  )
}
