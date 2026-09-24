import { useMemo, useState } from 'react'

import type { IncidentSummary, Scenario } from '../api/client'

interface Props {
  incidents: IncidentSummary[]
  selectedId: string | null
  scenarios: Scenario[]
  onSelect: (id: string) => void
}

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-slate-700/60 text-slate-300',
  running: 'bg-sky-500/15 text-sky-300',
  complete: 'bg-emerald-500/15 text-emerald-300',
  failed: 'bg-rose-500/15 text-rose-300',
}

function relativeTime(iso: string): string {
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 60) return `${Math.max(seconds, 0)}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

/** Past runs, filterable by scenario and outcome. */
export default function IncidentHistory({ incidents, selectedId, scenarios, onSelect }: Props) {
  const [scenarioFilter, setScenarioFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')

  const filtered = useMemo(
    () =>
      incidents.filter(
        (incident) =>
          (!scenarioFilter || incident.scenario_type === scenarioFilter) &&
          (!statusFilter || incident.status === statusFilter),
      ),
    [incidents, scenarioFilter, statusFilter],
  )

  const selectClass =
    'flex-1 rounded-md border border-slate-800 bg-slate-950 px-2 py-1 text-[11px] text-slate-300 outline-none focus:border-slate-600'

  return (
    // flex-1 + min-h-0 so the list grows into the sidebar and scrolls inside
    // itself, rather than collapsing to a few rows.
    <div className="card flex min-h-[18rem] flex-1 flex-col p-3 lg:min-h-0">
      <div className="mb-2 flex items-baseline justify-between px-1">
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          History
        </h2>
        <span className="text-[10px] text-slate-600">
          {filtered.length}
          {filtered.length !== incidents.length && `/${incidents.length}`}
        </span>
      </div>

      <div className="mb-2 flex gap-1.5">
        <select
          value={scenarioFilter}
          onChange={(event) => setScenarioFilter(event.target.value)}
          className={selectClass}
        >
          <option value="">all scenarios</option>
          {scenarios.map((scenario) => (
            <option key={scenario.key} value={scenario.key}>
              {scenario.key}
            </option>
          ))}
        </select>
        <select
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value)}
          className={selectClass}
        >
          <option value="">any outcome</option>
          <option value="complete">complete</option>
          <option value="pending">pending</option>
          <option value="running">running</option>
          <option value="failed">failed</option>
        </select>
      </div>

      <ul className="min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-1">
        {filtered.length === 0 && (
          <li className="py-6 text-center text-xs text-slate-600">
            {incidents.length === 0 ? 'No incidents yet.' : 'Nothing matches those filters.'}
          </li>
        )}
        {filtered.map((incident) => (
          <li key={incident.id}>
            <button
              type="button"
              onClick={() => onSelect(incident.id)}
              className={`w-full rounded-lg border px-2.5 py-2 text-left transition ${
                incident.id === selectedId
                  ? 'border-sky-500/40 bg-sky-500/5'
                  : 'border-slate-800 bg-slate-950/40 hover:border-slate-700'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-[11px] text-slate-300">
                  {incident.scenario_type}
                </span>
                <span className={`chip shrink-0 ${STATUS_STYLES[incident.status]}`}>
                  {incident.status}
                </span>
              </div>
              <div className="mt-1 flex items-center justify-between gap-2">
                <span className="text-[10px] text-slate-600">
                  {relativeTime(incident.created_at)}
                </span>
                {incident.confidence != null && (
                  <span className="font-mono text-[10px] text-agent-cause">
                    {Math.round(incident.confidence * 100)}%
                  </span>
                )}
              </div>
              {incident.root_cause && (
                <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-slate-500">
                  {incident.root_cause}
                </p>
              )}
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
