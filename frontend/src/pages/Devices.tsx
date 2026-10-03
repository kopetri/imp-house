import { useEffect, useState, type FormEvent } from 'react'
import { api, formatDate, isAbort, type Device } from '../api'

export default function Devices() {
  const [devices, setDevices] = useState<Device[]>([])
  const [name, setName] = useState('')
  const [token, setToken] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    api<Device[]>('/api/devices', { signal: controller.signal })
      .then(setDevices)
      .catch((err) => {
        if (!isAbort(err)) setError((err as Error).message)
      })
    return () => controller.abort()
  }, [version])
  const load = () => setVersion((v) => v + 1)

  const create = async (event: FormEvent) => {
    event.preventDefault()
    try {
      const created = await api<Device & { token: string }>('/api/devices', { method: 'POST', body: { name } })
      setToken(created.token)
      setName('')
      setError(null)
      load()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const remove = async (device: Device) => {
    if (!window.confirm(`Revoke “${device.name}”? It will lose access immediately.`)) return
    try {
      await api(`/api/devices/${device.id}`, { method: 'DELETE' })
      load()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <>
      <h1>Devices</h1>
      <p className="muted">Display devices (CYD) authenticate with a token to list and play augmented clips.</p>
      <form className="panel row" onSubmit={create}>
        <input placeholder="Device name" required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
        <button>Create token</button>
      </form>
      {token && (
        <div className="panel stack">
          <strong>Copy now, it will not be shown again.</strong>
          <div className="token">{token}</div>
          <span className="muted">
            Put it into <code>cyd-display/include/secrets.h</code> as <code>IMP_DEVICE_TOKEN</code>.
          </span>
          <div className="row">
            <button onClick={() => navigator.clipboard?.writeText(token)}>Copy</button>
            <button className="secondary" onClick={() => setToken(null)}>
              Done
            </button>
          </div>
        </div>
      )}
      {error && <p className="error">{error}</p>}
      <div className="panel">
        {devices.length === 0 ? (
          <p className="muted">No devices.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Created</th>
                <th>Last seen</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {devices.map((device) => (
                <tr key={device.id}>
                  <td>{device.name}</td>
                  <td>{formatDate(device.created_at)}</td>
                  <td>{device.last_seen_at ? formatDate(device.last_seen_at) : 'never'}</td>
                  <td>
                    <button className="danger" onClick={() => remove(device)}>
                      Revoke
                    </button>
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
