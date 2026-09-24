import type { Scenario } from '../api/client'

interface Props {
  scenarios: Scenario[]
  busy: string | null
  onSimulate: (key: string) => void
}

const ICONS: Record<string, string> = {
  bad_deploy: '⇧',
  memory_leak: '▲',
  slow_query: '⏱',
  conn_pool_exhaustion: '⛔',
}

/**
 * Drop the trailing parenthetical from a scenario title.
 *
 * The backend titles name the service — "Bad deploy (payments-service v2.4.1)"
 * — but the origin service is already on the next line, so at sidebar width
 * the parenthetical only causes truncation. The full title stays in the
 * tooltip.
 */
function shortTitle(title: string): string {
  return title.replace(/\s*\([^)]*\)\s*$/, '')
}

/**
 * The simulate panel: one button per injectable failure scenario.
 *
 * Each row is a single control -- title, the service the failure starts in,
 * and a two-line summary of what gets injected -- so the panel reads as one
 * list rather than a stack of nested boxes.
 */
export default function SimulateButton({ scenarios, busy, onSimulate }: Props) {
  if (scenarios.length === 0) {
    return (
      <div className="card shrink-0 p-4 text-sm text-slate-500">
        No scenarios available — is the backend running?
      </div>
    )
  }

  return (
    <div className="card shrink-0 p-3">
      <div className="mb-2 flex items-baseline justify-between px-1">
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          Simulate an incident
        </h2>
        <span className="text-[10px] text-slate-600">{scenarios.length}</span>
      </div>

      <div className="space-y-1">
        {scenarios.map((scenario) => {
          const isBusy = busy === scenario.key
          return (
            <button
              key={scenario.key}
              type="button"
              disabled={Boolean(busy)}
              onClick={() => onSimulate(scenario.key)}
              title={`${scenario.title} — ${scenario.description}`}
              className="group flex w-full items-start gap-2.5 rounded-lg border border-transparent px-2 py-2 text-left transition hover:border-slate-700 hover:bg-slate-800/40 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <span className="mt-0.5 w-4 shrink-0 text-center text-sm leading-none text-slate-500 group-hover:text-sky-400">
                {ICONS[scenario.key] ?? '•'}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-medium text-slate-200">
                  {shortTitle(scenario.title)}
                </span>
                <span className="mt-0.5 block truncate font-mono text-[10px] text-slate-500">
                  origin: {scenario.primary_service}
                </span>
              </span>
              <span className="mt-0.5 w-8 shrink-0 text-right text-[10px] text-slate-600 group-hover:text-sky-400">
                {isBusy ? (
                  <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-sky-400 border-t-transparent align-middle" />
                ) : (
                  'run →'
                )}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
