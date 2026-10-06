import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, formatDate, isAbort, type ScheduledRecording } from '../api'
import { useAuth } from '../auth'

export default function Dashboard() {
  const { user } = useAuth()
  const [connected, setConnected] = useState<boolean | null>(null)
  const [schedule, setSchedule] = useState<ScheduledRecording[]>([])
  const [message, setMessage] = useState<string | null>(null)
  const [streamKey, setStreamKey] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    const poll = () =>
      api<{ connected: boolean }>('/api/live/status', { signal: controller.signal })
        .then((s) => setConnected(s.connected))
        .catch(() => {})
    poll()
    api<ScheduledRecording[]>('/api/clips/schedule', { signal: controller.signal })
      .then(setSchedule)
      .catch((err) => {
        if (!isAbort(err)) setMessage((err as Error).message)
      })
    const timer = window.setInterval(poll, 5000)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [])

  const recordNow = async () => {
    setMessage(null)
    try {
      await api('/api/clips/record', { method: 'POST' })
      setMessage('Capturing snapshot…')
    } catch (err) {
      setMessage((err as Error).message)
    }
  }

  return (
    <>
      <h1>Live</h1>
      <div className="panel">
        <img
          key={streamKey}
          className="live"
          src={`/api/live.mjpeg?k=${streamKey}`}
          alt="Live camera stream"
          onError={() => window.setTimeout(() => setStreamKey((k) => k + 1), 3000)}
        />
        <p className="muted">
          Camera:{' '}
          {connected === null ? '…' : connected ? <span className="success">connected</span> : 'connecting'}
        </p>
        {user?.is_admin && (
          <div className="row">
            <button onClick={recordNow}>Capture snapshot</button>
          </div>
        )}
        {message && <p className="muted">{message}</p>}
      </div>

      <h2>Today&apos;s schedule</h2>
      <div className="panel">
        {schedule.length === 0 ? (
          <p className="muted">No recordings planned for today.</p>
        ) : (
          <table>
            <tbody>
              {schedule.map((entry) => (
                <tr key={entry.id}>
                  <td>{formatDate(entry.scheduled_at)}</td>
                  <td>
                    <span className={`badge ${entry.status}`}>{entry.status}</span>
                  </td>
                  <td>
                    {entry.clip_id ? <Link to={`/clips/${entry.clip_id}`}>clip</Link> : entry.error ?? ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
