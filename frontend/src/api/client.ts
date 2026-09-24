/**
 * Typed client for the Incident Sentinel API.
 *
 * In development Vite proxies `/api` to the backend, so the browser sees one
 * origin and SSE needs no CORS handling. In production VITE_API_URL points at
 * the deployed backend.
 */

const BASE = (import.meta.env.VITE_API_URL ?? '/api').replace(/\/$/, '')

// --------------------------------------------------------------------------
// Types (mirroring backend/app/schemas.py)
// --------------------------------------------------------------------------

export type IncidentStatus = 'pending' | 'running' | 'complete' | 'failed'

export interface Scenario {
  key: string
  title: string
  description: string
  primary_service: string
  affected_services: string[]
}

export interface IncidentSummary {
  id: string
  scenario_type: string
  status: IncidentStatus
  triggered_at: string
  created_at: string
  root_cause: string | null
  confidence: number | null
}

export interface IncidentDetail extends IncidentSummary {
  seed: number
  window_start: string
  window_end: string
  log_count: number
  metric_count: number
  evidence_count: number
  trace_step_count: number
}

export interface SimulateResponse {
  incident_id: string
  scenario_type: string
  status: IncidentStatus
  seed: number
  triggered_at: string
  window_start: string
  window_end: string
  services: string[]
  log_count: number
  metric_count: number
}

export interface Evidence {
  id: string
  source: 'log' | 'metric'
  service: string
  excerpt: string
  timestamp: string
  source_ref: string | null
}

export interface TraceStep {
  id: string
  agent_name: string
  step_order: number
  input_summary: string | null
  output_summary: string | null
  reasoning: string | null
  evidence_refs: string[]
  duration_ms: number | null
  created_at: string
}

export interface TraceResponse {
  incident_id: string
  status: IncidentStatus
  root_cause: string | null
  confidence: number | null
  steps: TraceStep[]
  evidence: Evidence[]
}

export interface Candidate {
  cause: string
  service: string
  confidence: number
  why: string
  evidence_refs: string[]
  service_known?: boolean
}

export interface FixStep {
  order: number
  action: string
  kind: 'mitigate' | 'fix' | 'verify'
  rationale: string
  risk: string
}

export interface TriageResult {
  incident_id: string
  status: IncidentStatus
  root_cause: string | null
  confidence: number | null
  steps: number
  candidates: Candidate[]
  fix: { steps?: FixStep[]; prevention?: string; service?: string }
  trace?: StreamedStep[]
  duration_ms: number
  model: string
  cached: boolean
}

/** A step as it arrives over SSE (richer than the stored row: it carries findings). */
export interface StreamedStep {
  agent_name: string
  summary: string
  reasoning: string
  findings: Record<string, unknown>
  evidence_refs: string[]
  duration_ms: number
  model: string
  step_order?: number
}

export interface MetricPoint {
  timestamp: string
  service: string
  metric: string
  value: number
}

export interface LogLine {
  id: string
  timestamp: string
  service: string
  level: string
  message: string
  trace_id: string
  attrs: Record<string, unknown>
}

export interface Health {
  status: string
  environment: string
  database: string
  cache: string
  llm_provider: string
  llm_available: boolean
  llm_model: string
  scenarios: number
}

// --------------------------------------------------------------------------
// Requests
// --------------------------------------------------------------------------

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* response had no JSON body; the status line is all we have */
    }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<Health>('/health'),

  scenarios: () => request<Scenario[]>('/scenarios'),

  simulate: (scenario_type: string, seed?: number) =>
    request<SimulateResponse>('/simulate', {
      method: 'POST',
      body: JSON.stringify({ scenario_type, ...(seed !== undefined ? { seed } : {}) }),
    }),

  incidents: (params: { limit?: number; scenario_type?: string; status?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.limit) query.set('limit', String(params.limit))
    if (params.scenario_type) query.set('scenario_type', params.scenario_type)
    if (params.status) query.set('status', params.status)
    const suffix = query.toString() ? `?${query}` : ''
    return request<IncidentSummary[]>(`/incidents${suffix}`)
  },

  incident: (id: string) => request<IncidentDetail>(`/incidents/${id}`),

  trace: (id: string) => request<TraceResponse>(`/incidents/${id}/trace`),

  triage: (id: string, options: { provider?: string; refresh?: boolean } = {}) =>
    request<TriageResult>(`/incidents/${id}/triage`, {
      method: 'POST',
      body: JSON.stringify(options),
    }),

  metrics: (id: string, service: string, metric: string) =>
    request<MetricPoint[]>(
      `/incidents/${id}/metrics?service=${encodeURIComponent(service)}&metric=${encodeURIComponent(metric)}`,
    ),

  logs: (id: string, params: { service?: string; level?: string; limit?: number } = {}) => {
    const query = new URLSearchParams()
    if (params.service) query.set('service', params.service)
    if (params.level) query.set('level', params.level)
    query.set('limit', String(params.limit ?? 50))
    return request<LogLine[]>(`/incidents/${id}/logs?${query}`)
  },
}

// --------------------------------------------------------------------------
// Streaming
// --------------------------------------------------------------------------

export interface TriageStreamHandlers {
  onStatus?: (payload: { state: string; provider?: string; model?: string }) => void
  onStep?: (step: StreamedStep) => void
  onComplete?: (result: TriageResult) => void
  onError?: (message: string) => void
}

/**
 * Subscribe to a live triage run. Returns a function that closes the stream.
 *
 * EventSource only issues GET requests, which is why the streaming endpoint is
 * a GET while the plain JSON triage stays a POST.
 */
export function streamTriage(
  incidentId: string,
  handlers: TriageStreamHandlers,
  options: { provider?: string; refresh?: boolean } = {},
): () => void {
  const query = new URLSearchParams()
  if (options.provider) query.set('provider', options.provider)
  if (options.refresh) query.set('refresh', 'true')

  const source = new EventSource(`${BASE}/incidents/${incidentId}/triage/stream?${query}`)
  let finished = false

  const parse = <T,>(event: MessageEvent): T | null => {
    try {
      return JSON.parse(event.data) as T
    } catch {
      return null
    }
  }

  source.addEventListener('status', (event) => {
    const payload = parse<{ state: string; provider?: string; model?: string }>(event as MessageEvent)
    if (payload) handlers.onStatus?.(payload)
  })

  source.addEventListener('step', (event) => {
    const payload = parse<StreamedStep>(event as MessageEvent)
    if (payload) handlers.onStep?.(payload)
  })

  source.addEventListener('complete', (event) => {
    const payload = parse<TriageResult>(event as MessageEvent)
    finished = true
    if (payload) handlers.onComplete?.(payload)
    source.close()
  })

  source.addEventListener('error', (event) => {
    // The server sends a named `error` event with a detail payload; the browser
    // also fires a bare `error` on transport failure, which has no data.
    const payload = parse<{ detail: string }>(event as MessageEvent)
    if (payload?.detail) {
      finished = true
      handlers.onError?.(payload.detail)
      source.close()
      return
    }
    if (!finished && source.readyState === EventSource.CLOSED) {
      handlers.onError?.('Connection to the triage stream was lost.')
    }
  })

  return () => {
    finished = true
    source.close()
  }
}

export const AGENT_LABELS: Record<string, string> = {
  log_analysis: 'Log Analysis',
  metrics_correlation: 'Metrics Correlation',
  root_cause: 'Root-Cause Reasoning',
  fix_suggestion: 'Fix Suggestion',
}

export const AGENT_ORDER = ['log_analysis', 'metrics_correlation', 'root_cause', 'fix_suggestion']

export const AGENT_COLORS: Record<string, { text: string; bg: string; ring: string; rail: string }> = {
  log_analysis: { text: 'text-agent-log', bg: 'bg-agent-log/10', ring: 'ring-agent-log/40', rail: 'bg-agent-log' },
  metrics_correlation: {
    text: 'text-agent-metrics',
    bg: 'bg-agent-metrics/10',
    ring: 'ring-agent-metrics/40',
    rail: 'bg-agent-metrics',
  },
  root_cause: { text: 'text-agent-cause', bg: 'bg-agent-cause/10', ring: 'ring-agent-cause/40', rail: 'bg-agent-cause' },
  fix_suggestion: { text: 'text-agent-fix', bg: 'bg-agent-fix/10', ring: 'ring-agent-fix/40', rail: 'bg-agent-fix' },
}
