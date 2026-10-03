import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, formatDate, isAbort, type Clip } from '../api'
import { useAuth } from '../auth'

export default function ClipDetail() {
  const { id } = useParams<{ id: string }>()
  const { user } = useAuth()
  const navigate = useNavigate()
  const [clip, setClip] = useState<Clip | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    api<Clip>(`/api/clips/${id}`, { signal: controller.signal })
      .then(setClip)
      .catch((err) => {
        if (!isAbort(err)) setError((err as Error).message)
      })
    return () => controller.abort()
  }, [id, tick])

  const busy = clip?.status === 'queued' || clip?.status === 'processing'
  useEffect(() => {
    if (!busy) return
    const timer = window.setInterval(() => setTick((t) => t + 1), 15000)
    return () => window.clearInterval(timer)
  }, [busy])

  if (error) return <p className="error">{error}</p>
  if (!clip) return <p className="muted">Loading…</p>

  const augment = async () => {
    try {
      setClip(await api<Clip>(`/api/clips/${clip.id}/augment`, { method: 'POST' }))
      setError(null)
    } catch (err) {
      window.alert((err as Error).message)
    }
  }

  const remove = async () => {
    if (!window.confirm('Delete this clip and all its files?')) return
    try {
      await api(`/api/clips/${clip.id}`, { method: 'DELETE' })
      navigate('/clips')
    } catch (err) {
      window.alert((err as Error).message)
    }
  }

  return (
    <>
      <h1>{formatDate(clip.recorded_at)}</h1>
      <div className="videos">
        <div>
          <h2>Original</h2>
          <video src={`/api/clips/${clip.id}/original.mp4`} controls loop playsInline />
        </div>
        <div>
          <h2>Augmented</h2>
          {clip.has_augmented ? (
            <video src={`/api/clips/${clip.id}/augmented.mp4`} controls loop playsInline />
          ) : (
            <p className="muted">Not available yet.</p>
          )}
        </div>
      </div>
      <div className="panel" style={{ marginTop: '1rem' }}>
        <table>
          <tbody>
            <tr>
              <th>Status</th>
              <td>
                <span className={`badge ${clip.status}`}>{clip.status}</span> (attempts: {clip.attempts})
              </td>
            </tr>
            <tr>
              <th>Duration</th>
              <td>{clip.duration_seconds.toFixed(1)}s</td>
            </tr>
            <tr>
              <th>Prompt</th>
              <td>
                <strong>{clip.prompt_name ?? '—'}</strong>
                {clip.prompt_text && <pre className="prompt">{clip.prompt_text}</pre>}
              </td>
            </tr>
            <tr>
              <th>Model</th>
              <td>{clip.model ?? '—'}</td>
            </tr>
            {clip.error && (
              <tr>
                <th>Error</th>
                <td className="error">{clip.error}</td>
              </tr>
            )}
          </tbody>
        </table>
        {user?.is_admin && (
          <div className="row" style={{ marginTop: '1rem' }}>
            <button onClick={augment} disabled={busy}>
              Augment with active prompt
            </button>
            <button className="danger" onClick={remove} disabled={clip.status === 'processing'}>
              Delete
            </button>
          </div>
        )}
      </div>
    </>
  )
}
