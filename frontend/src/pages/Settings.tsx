import { useEffect, useState, type FormEvent } from 'react'
import { api, isAbort, type RecordingSettings } from '../api'
import { useAuth } from '../auth'

const asTime = (value: string) => (value.length === 5 ? `${value}:00` : value)

export default function Settings() {
  const { user } = useAuth()
  const [form, setForm] = useState<RecordingSettings | null>(null)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    api<RecordingSettings>('/api/settings', { signal: controller.signal })
      .then(setForm)
      .catch((err) => {
        if (!isAbort(err)) setMessage({ ok: false, text: (err as Error).message })
      })
    return () => controller.abort()
  }, [])

  if (!form) return message ? <p className="error">{message.text}</p> : <p className="muted">Loading…</p>

  const readOnly = !user?.is_admin
  const number = (key: keyof RecordingSettings) => (e: { target: { value: string } }) =>
    setForm({ ...form, [key]: Number(e.target.value) })

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    try {
      const body = { ...form, window_start: asTime(form.window_start), window_end: asTime(form.window_end) }
      setForm(await api<RecordingSettings>('/api/settings', { method: 'PUT', body }))
      setMessage({ ok: true, text: "Saved. Today's remaining recordings were re-planned." })
    } catch (err) {
      setMessage({ ok: false, text: (err as Error).message })
    }
  }

  return (
    <>
      <h1>Recording schedule</h1>
      <form className="panel stack" onSubmit={submit}>
        <fieldset disabled={readOnly} className="stack" style={{ border: 'none', padding: 0, margin: 0 }}>
          <label className="inline">
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
            Random recording enabled
          </label>
          <label>
            Clips per day
            <input type="number" min={0} max={50} value={form.clips_per_day} onChange={number('clips_per_day')} />
          </label>
          <div className="row">
            <label>
              Window start
              <input
                type="time"
                step={1}
                value={form.window_start}
                onChange={(e) => setForm({ ...form, window_start: e.target.value })}
              />
            </label>
            <label>
              Window end
              <input
                type="time"
                step={1}
                value={form.window_end}
                onChange={(e) => setForm({ ...form, window_end: e.target.value })}
              />
            </label>
          </div>
          {!readOnly && <button>Save</button>}
        </fieldset>
        {readOnly && <p className="muted">Only admins can change the schedule.</p>}
        {message && <p className={message.ok ? 'success' : 'error'}>{message.text}</p>}
      </form>
    </>
  )
}
