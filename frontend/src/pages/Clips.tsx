import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, formatDate, isAbort, type Clip, type ClipStatus } from '../api'

const FILTERS: Array<ClipStatus | 'all'> = ['all', 'done', 'queued', 'processing', 'failed', 'recorded']
const PAGE_SIZE = 24

export default function Clips() {
  const [filter, setFilter] = useState<ClipStatus | 'all'>('all')
  const [offset, setOffset] = useState(0)
  const [clips, setClips] = useState<Clip[]>([])
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) })
    if (filter !== 'all') query.set('status', filter)
    api<{ items: Clip[]; total: number }>(`/api/clips?${query}`, { signal: controller.signal })
      .then((page) => {
        setClips(page.items)
        setTotal(page.total)
        setError(null)
      })
      .catch((err) => {
        if (!isAbort(err)) setError((err as Error).message)
      })
    return () => controller.abort()
  }, [filter, offset, tick])

  const busy = clips.some((c) => c.status === 'queued' || c.status === 'processing')
  useEffect(() => {
    if (!busy) return
    const timer = window.setInterval(() => setTick((t) => t + 1), 15000)
    return () => window.clearInterval(timer)
  }, [busy])

  return (
    <>
      <h1>Clips</h1>
      <div className="tabs">
        {FILTERS.map((f) => (
          <button
            key={f}
            className={f === filter ? 'active' : ''}
            onClick={() => {
              setFilter(f)
              setOffset(0)
            }}
          >
            {f}
          </button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
      {clips.length === 0 && !error && <p className="muted">No clips yet.</p>}
      <div className="grid">
        {clips.map((clip) => (
          <Link key={clip.id} to={`/clips/${clip.id}`} className="card">
            <img src={`/api/clips/${clip.id}/thumb.jpg`} alt="" loading="lazy" />
            <div className="meta">
              <span>{formatDate(clip.recorded_at)}</span>
              <span className="muted">{clip.prompt_name ?? 'no prompt'} · {Math.round(clip.duration_seconds)}s</span>
              <span className={`badge ${clip.status}`}>{clip.status}</span>
            </div>
          </Link>
        ))}
      </div>
      {total > PAGE_SIZE && (
        <div className="row" style={{ marginTop: '1rem' }}>
          <button className="secondary" disabled={offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>
            Previous
          </button>
          <span className="muted">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
          </span>
          <button
            className="secondary"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next
          </button>
        </div>
      )}
    </>
  )
}
