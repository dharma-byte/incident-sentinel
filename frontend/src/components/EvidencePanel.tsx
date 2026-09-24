import { useEffect, useState } from 'react'

import { api, type Evidence, type MetricPoint } from '../api/client'

interface Props {
  incidentId: string
  evidence: Evidence[]
  emptyLabel?: string
}

/** A metric citation names its series, e.g. "latency_p95_ms 18.3 -> 1499.1 (x82)". */
function metricName(excerpt: string): string | null {
  const match = excerpt.match(/^([a-z0-9_]+)\s/i)
  return match ? match[1] : null
}

function Sparkline({ points, onset }: { points: MetricPoint[]; onset?: number }) {
  if (points.length < 2) return null

  const values = points.map((p) => p.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const width = 560
  const height = 72

  const path = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width
      const y = height - ((value - min) / span) * (height - 8) - 4
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')

  const onsetX = onset !== undefined ? (onset / (values.length - 1)) * width : null

  return (
    <div className="mt-2 overflow-x-auto">
      <svg viewBox={`0 0 ${width} ${height}`} className="h-20 w-full min-w-[320px]" role="img">
        <defs>
          <linearGradient id="spark" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(167 139 250)" stopOpacity="0.35" />
            <stop offset="100%" stopColor="rgb(167 139 250)" stopOpacity="0" />
          </linearGradient>
        </defs>
        {onsetX !== null && (
          <line
            x1={onsetX}
            y1={0}
            x2={onsetX}
            y2={height}
            stroke="rgb(251 146 60)"
            strokeWidth="1"
            strokeDasharray="3 3"
          />
        )}
        <path d={`${path} L${width},${height} L0,${height} Z`} fill="url(#spark)" />
        <path d={path} fill="none" stroke="rgb(167 139 250)" strokeWidth="1.5" />
      </svg>
      <div className="flex justify-between font-mono text-[10px] text-slate-600">
        <span>min {min.toFixed(1)}</span>
        {onsetX !== null && <span className="text-agent-cause">| incident onset</span>}
        <span>max {max.toFixed(1)}</span>
      </div>
    </div>
  )
}

function MetricChart({ incidentId, item }: { incidentId: string; item: Evidence }) {
  const [points, setPoints] = useState<MetricPoint[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const metric = metricName(item.excerpt)

  useEffect(() => {
    if (!metric) return
    let active = true
    api
      .metrics(incidentId, item.service, metric)
      .then((data) => active && setPoints(data))
      .catch((err: Error) => active && setError(err.message))
    return () => {
      active = false
    }
  }, [incidentId, item.service, metric])

  if (!metric) return null
  if (error) return <p className="mt-2 text-[11px] text-rose-400">Could not load series: {error}</p>
  if (!points) return <p className="mt-2 text-[11px] text-slate-600">loading series…</p>

  // The cited sample marks where the metric first mattered.
  const citedIndex = points.findIndex((p) => p.timestamp === item.timestamp)
  return <Sparkline points={points} onset={citedIndex >= 0 ? citedIndex : undefined} />
}

/**
 * The evidence a step cited, expandable to the underlying data.
 *
 * Every row here resolves to a stored `incident_logs` or `incident_metrics`
 * row, so a reviewer can check a claim rather than take it on trust.
 */
export default function EvidencePanel({ incidentId, evidence, emptyLabel }: Props) {
  const [open, setOpen] = useState<string | null>(null)

  if (evidence.length === 0) {
    return (
      <p className="text-xs italic text-slate-600">{emptyLabel ?? 'No evidence cited.'}</p>
    )
  }

  return (
    <ul className="space-y-1.5">
      {evidence.map((item) => {
        const isOpen = open === item.id
        const isMetric = item.source === 'metric'
        return (
          <li
            key={item.id}
            className="rounded-lg border border-slate-800 bg-slate-950/60 transition hover:border-slate-700"
          >
            <button
              type="button"
              onClick={() => setOpen(isOpen ? null : item.id)}
              className="flex w-full items-start gap-2 px-2.5 py-2 text-left"
            >
              <span
                className={`chip mt-0.5 shrink-0 ${
                  isMetric ? 'bg-agent-metrics/15 text-agent-metrics' : 'bg-agent-log/15 text-agent-log'
                }`}
              >
                {isMetric ? 'metric' : 'log'}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block font-mono text-[11px] text-slate-500">
                  {item.service} · {new Date(item.timestamp).toLocaleTimeString()}
                </span>
                <span
                  className={`mt-0.5 block break-words font-mono text-xs text-slate-300 ${
                    isOpen ? '' : 'line-clamp-2'
                  }`}
                >
                  {item.excerpt}
                </span>
              </span>
              <span className="mt-0.5 shrink-0 text-[10px] text-slate-600">
                {isOpen ? '▲' : '▼'}
              </span>
            </button>

            {isOpen && (
              <div className="animate-fade-up border-t border-slate-800 px-2.5 py-2">
                {isMetric ? (
                  <MetricChart incidentId={incidentId} item={item} />
                ) : (
                  <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-slate-400">
                    {item.excerpt}
                  </pre>
                )}
                <p className="mt-2 font-mono text-[10px] text-slate-600">
                  {item.source} row {item.source_ref ?? '(unlinked)'}
                </p>
              </div>
            )}
          </li>
        )
      })}
    </ul>
  )
}
