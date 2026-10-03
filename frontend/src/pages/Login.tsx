import { useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { api, type User } from '../api'
import { useAuth } from '../auth'

export function AuthForm({ mode }: { mode: 'login' | 'signup' }) {
  const { user, setUser } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (user) return <Navigate to="/" replace />

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      setUser(await api<User>(`/api/auth/${mode}`, { method: 'POST', body: { email, password } }))
      navigate('/')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const isLogin = mode === 'login'
  return (
    <div className="panel auth-box">
      <h1>{isLogin ? 'Log in' : 'Create account'}</h1>
      <form className="stack" onSubmit={submit}>
        <label>
          Email
          <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete={isLogin ? 'current-password' : 'new-password'}
            minLength={isLogin ? 1 : 10}
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {!isLogin && <span className="muted">At least 10 characters.</span>}
        {error && <p className="error">{error}</p>}
        <button disabled={busy}>{isLogin ? 'Log in' : 'Sign up'}</button>
      </form>
      <p className="muted">
        {isLogin ? (
          <>No account? <Link to="/signup">Sign up</Link></>
        ) : (
          <>Already registered? <Link to="/login">Log in</Link></>
        )}
      </p>
    </div>
  )
}

export default function Login() {
  return <AuthForm mode="login" />
}
