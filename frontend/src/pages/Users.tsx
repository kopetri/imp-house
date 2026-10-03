import { useEffect, useState } from 'react'
import { api, isAbort, type User } from '../api'
import { useAuth } from '../auth'

export default function Users() {
  const { user: me } = useAuth()
  const [users, setUsers] = useState<User[]>([])
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    api<User[]>('/api/users', { signal: controller.signal })
      .then(setUsers)
      .catch((err) => {
        if (!isAbort(err)) setError((err as Error).message)
      })
    return () => controller.abort()
  }, [version])

  const update = async (user: User, body: Partial<Pick<User, 'is_active' | 'is_admin'>>) => {
    try {
      await api(`/api/users/${user.id}`, { method: 'PATCH', body })
      setError(null)
      setVersion((v) => v + 1)
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <>
      <h1>Users</h1>
      {error && <p className="error">{error}</p>}
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Email</th>
              <th>Active</th>
              <th>Admin</th>
            </tr>
          </thead>
          <tbody>
            {users.map((user) => (
              <tr key={user.id}>
                <td>{user.email}</td>
                <td>
                  <input
                    type="checkbox"
                    checked={user.is_active}
                    disabled={user.id === me?.id}
                    onChange={(e) => update(user, { is_active: e.target.checked })}
                  />
                </td>
                <td>
                  <input
                    type="checkbox"
                    checked={user.is_admin}
                    disabled={user.id === me?.id}
                    onChange={(e) => update(user, { is_admin: e.target.checked })}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
