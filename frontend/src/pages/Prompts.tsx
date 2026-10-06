import { useEffect, useState, type FormEvent } from 'react'
import { api, isAbort, type Prompt, type PromptInput } from '../api'
import { useAuth } from '../auth'

const EMPTY: PromptInput = {
  name: '',
  text: '',
}

function PromptForm({
  initial,
  onSaved,
  onCancel,
}: {
  initial: Prompt | null
  onSaved: () => void
  onCancel: () => void
}) {
  const [form, setForm] = useState<PromptInput>(initial ?? EMPTY)
  const [error, setError] = useState<string | null>(null)

  const update = (key: keyof PromptInput) => (e: { target: { value: string } }) =>
    setForm({ ...form, [key]: e.target.value })

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    try {
      await api(initial ? `/api/prompts/${initial.id}` : '/api/prompts', {
        method: initial ? 'PUT' : 'POST',
        body: form,
      })
      onSaved()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <form className="panel stack" onSubmit={submit}>
      <h2>{initial ? `Edit “${initial.name}”` : 'New prompt'}</h2>
      <label>
        Name
        <input required maxLength={100} value={form.name} onChange={update('name')} />
      </label>
      <label>
        Character description
        <textarea required maxLength={4000} value={form.text} onChange={update('text')} />
      </label>
      {error && <p className="error">{error}</p>}
      <div className="row">
        <button>Save</button>
        <button type="button" className="secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  )
}

export default function Prompts() {
  const { user } = useAuth()
  const [prompts, setPrompts] = useState<Prompt[]>([])
  const [editing, setEditing] = useState<Prompt | 'new' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    api<Prompt[]>('/api/prompts', { signal: controller.signal })
      .then(setPrompts)
      .catch((err) => {
        if (!isAbort(err)) setError((err as Error).message)
      })
    return () => controller.abort()
  }, [version])
  const load = () => setVersion((v) => v + 1)

  const action = async (path: string, method = 'POST') => {
    try {
      await api(path, { method })
      setError(null)
      load()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const isAdmin = user?.is_admin ?? false
  return (
    <>
      <h1>Prompts</h1>
      <p className="muted">The active prompt is applied to every newly recorded clip.</p>
      {error && <p className="error">{error}</p>}
      {isAdmin && editing === null && (
        <button onClick={() => setEditing('new')} style={{ marginBottom: '1rem' }}>
          New prompt
        </button>
      )}
      {isAdmin && editing !== null && (
        <PromptForm
          key={editing === 'new' ? 'new' : editing.id}
          initial={editing === 'new' ? null : editing}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            load()
          }}
        />
      )}
      {prompts.map((prompt) => (
        <div key={prompt.id} className="panel stack">
          <div className="row">
            <strong>{prompt.name}</strong>
            {prompt.is_active && <span className="badge active">active</span>}
            <span className="spacer" />
          </div>
          <pre className="prompt">{prompt.text}</pre>
          {isAdmin && (
            <div className="row">
              {prompt.is_active ? (
                <button className="secondary" onClick={() => action(`/api/prompts/${prompt.id}/deactivate`)}>
                  Deactivate
                </button>
              ) : (
                <button onClick={() => action(`/api/prompts/${prompt.id}/activate`)}>Activate</button>
              )}
              <button className="secondary" onClick={() => setEditing(prompt)}>
                Edit
              </button>
              <button
                className="danger"
                onClick={() => window.confirm(`Delete “${prompt.name}”?`) && action(`/api/prompts/${prompt.id}`, 'DELETE')}
              >
                Delete
              </button>
            </div>
          )}
        </div>
      ))}
      {prompts.length === 0 && <p className="muted">No prompts yet.</p>}
    </>
  )
}
