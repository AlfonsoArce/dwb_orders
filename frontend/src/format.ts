// Timestamps arrive as UTC and are read in Miami, so every one of them is
// rendered in Eastern time and says so. See ADR-0001.

const EASTERN = 'America/New_York'

const timestamp = new Intl.DateTimeFormat('en-US', {
  timeZone: EASTERN,
  dateStyle: 'medium',
  timeStyle: 'short',
})

export function easternTime(value: string | null): string {
  if (value === null) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '—'
  return `${timestamp.format(parsed)} ET`
}

export function count(value: number): string {
  return value.toLocaleString('en-US')
}
