// The shapes the viewer's API answers with. Kept in step with the pydantic
// models in dwb/viewer.py by hand: there is one consumer and one server, and
// generating a client would be more machinery than the contract is large.
//
// Money, when it arrives, crosses the wire as a string — JSON has no decimal
// type, and these are dollars on an invoice.

export type DatabaseHealth = {
  reachable: boolean
  problem: string | null
}

export type NewestOrder = {
  order_number: number
  placed_at: string | null
}

export type StoreFigures = {
  total_orders: number
  in_flight_orders: number
  newest_order: NewestOrder | null
  last_written_at: string | null
}

/** Absent `store` means the database could not be read, not that it is empty. */
export type Health = {
  database: DatabaseHealth
  store: StoreFigures | null
}

export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  const response = await fetch('/api/health', { signal })
  if (!response.ok) {
    throw new Error(`The viewer answered ${response.status}`)
  }
  return (await response.json()) as Health
}
