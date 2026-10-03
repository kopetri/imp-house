import type { ReactNode } from 'react'
import { Navigate, NavLink, Route, Routes, useNavigate } from 'react-router-dom'
import { useAuth } from './auth'
import ClipDetail from './pages/ClipDetail'
import Clips from './pages/Clips'
import Dashboard from './pages/Dashboard'
import Devices from './pages/Devices'
import Login from './pages/Login'
import Prompts from './pages/Prompts'
import Settings from './pages/Settings'
import Signup from './pages/Signup'
import Users from './pages/Users'

function Protected({ children, admin = false }: { children: ReactNode; admin?: boolean }) {
  const { user, loading } = useAuth()
  if (loading) return <p className="muted">Loading…</p>
  if (!user) return <Navigate to="/login" replace />
  if (admin && !user.is_admin) return <Navigate to="/" replace />
  return children
}

function Nav() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  if (!user) return null
  return (
    <nav className="nav">
      <span className="brand">imp-house</span>
      <NavLink to="/" end>Live</NavLink>
      <NavLink to="/clips">Clips</NavLink>
      <NavLink to="/prompts">Prompts</NavLink>
      <NavLink to="/settings">Schedule</NavLink>
      <NavLink to="/devices">Devices</NavLink>
      {user.is_admin && <NavLink to="/users">Users</NavLink>}
      <span className="spacer" />
      <span className="muted">{user.email}</span>
      <button
        className="secondary"
        onClick={async () => {
          await logout()
          navigate('/login')
        }}
      >
        Log out
      </button>
    </nav>
  )
}

export default function App() {
  return (
    <>
      <Nav />
      <main>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/signup" element={<Signup />} />
          <Route path="/" element={<Protected><Dashboard /></Protected>} />
          <Route path="/clips" element={<Protected><Clips /></Protected>} />
          <Route path="/clips/:id" element={<Protected><ClipDetail /></Protected>} />
          <Route path="/prompts" element={<Protected><Prompts /></Protected>} />
          <Route path="/settings" element={<Protected><Settings /></Protected>} />
          <Route path="/devices" element={<Protected><Devices /></Protected>} />
          <Route path="/users" element={<Protected admin><Users /></Protected>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </>
  )
}
