import { useEffect, useState } from 'react'

import { fetchHealth, type Health } from './api'
import { count, easternTime } from './format'

// The panel answers "has the fetcher kept up?", which stops being true while
// you look at it. Often enough to be current, rarely enough to be quiet.
const REFRESH_MS = 15_000

// 'status' and 'state' belong to an Order in this codebase (see CONTEXT.md),
// so what the browser is doing is a Request, and what it is doing is a phase.
type Request =
  | { phase: 'loading' }
  | { phase: 'ready'; health: Health }
  | { phase: 'failed'; problem: string }

function useHealth(): Request {
  const [request, setRequest] = useState<Request>({ phase: 'loading' })

  useEffect(() => {
    const controller = new AbortController()

    const load = async () => {
      try {
        const health = await fetchHealth(controller.signal)
        setRequest({ phase: 'ready', health })
      } catch (error) {
        if (controller.signal.aborted) return
        // The API reports an unreachable *database* itself; reaching this
        // means the viewer itself did not answer.
        setRequest({
          phase: 'failed',
          problem: error instanceof Error ? error.message : 'The viewer did not answer.',
        })
      }
    }

    void load()
    const timer = window.setInterval(() => void load(), REFRESH_MS)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [])

  return request
}

type FigureProps = {
  label: string
  value: string
  note?: string
  // A timestamp is several times longer than a count and does not want to be
  // set at the same size.
  long?: boolean
}

function Figure({ label, value, note, long = false }: FigureProps) {
  return (
    <div className="figure">
      <div className="figure-label">{label}</div>
      <div className={long ? 'figure-value figure-value-long' : 'figure-value'}>
        {value}
      </div>
      {note !== undefined && <div className="figure-note">{note}</div>}
    </div>
  )
}

function Store({ health }: { health: Health }) {
  if (!health.database.reachable || health.store === null) {
    return (
      <div className="problem" role="alert">
        <h2>The Order store cannot be reached</h2>
        <pre>{health.database.problem ?? 'No reason given.'}</pre>
      </div>
    )
  }

  const { store } = health
  const newest = store.newest_order

  return (
    <div className="figures">
      <Figure
        label="Newest Order"
        value={newest === null ? '—' : String(newest.order_number)}
        note={newest === null ? 'No Orders yet' : `Placed ${easternTime(newest.placed_at)}`}
      />
      <Figure
        label="In Flight"
        value={count(store.in_flight_orders)}
        note="Not yet Completed or Cancelled"
      />
      <Figure label="Orders" value={count(store.total_orders)} note="In the store" />
      <Figure
        label="Last written"
        value={easternTime(store.last_written_at)}
        note="When the store last changed"
        long
      />
    </div>
  )
}

export function App() {
  const request = useHealth()

  return (
    <main>
      <header>
        <h1>HawkExpress Orders</h1>
        <p className="read-only">Read-only. This viewer cannot change anything.</p>
      </header>

      {request.phase === 'loading' && <p className="quiet">Reading the store…</p>}
      {request.phase === 'failed' && (
        <div className="problem" role="alert">
          <h2>The viewer is not answering</h2>
          <pre>{request.problem}</pre>
        </div>
      )}
      {request.phase === 'ready' && <Store health={request.health} />}
    </main>
  )
}
