export interface User {
  id: number
  email: string
  is_admin: boolean
  is_active: boolean
}

export interface Device {
  id: number
  name: string
  owner_id: number
  created_at: string
  last_seen_at: string | null
}

export interface PromptInput {
  name: string
  text: string
}

export interface Prompt extends PromptInput {
  id: number
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface RecordingSettings {
  enabled: boolean
  clips_per_day: number
  window_start: string
  window_end: string
}

export type ClipStatus = 'recorded' | 'queued' | 'processing' | 'done' | 'failed'

export interface Clip {
  id: string
  recorded_at: string
  duration_seconds: number
  status: ClipStatus
  prompt_name: string | null
  prompt_text: string | null
  model: string | null
  attempts: number
  error: string | null
  has_snapshot: boolean
  has_original: boolean
  has_mask: boolean
  has_reference: boolean
  has_augmented: boolean
  scene_prompt: string | null
  processing_stage: string | null
  playback_frames: number | null
}

export interface ScheduledRecording {
  id: number
  scheduled_at: string
  status: 'pending' | 'done' | 'failed'
  clip_id: string | null
  error: string | null
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

let onUnauthorized: () => void = () => {}

export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler
}

function detailMessage(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail.map((item) => (item as { msg?: string }).msg ?? String(item)).join('; ')
    }
  }
  return fallback
}

export async function api<T = void>(
  path: string,
  options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = { 'X-Requested-With': 'imp-house' }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    credentials: 'same-origin',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  })
  if (response.status === 204) return undefined as T
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/api/auth/')) onUnauthorized()
    throw new ApiError(response.status, detailMessage(body, response.statusText))
  }
  return body as T
}

export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}
