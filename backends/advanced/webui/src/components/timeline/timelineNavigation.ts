const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/

function isCalendarDate(value: string) {
  if (!DATE_PATTERN.test(value)) return false
  const [year, month, day] = value.split('-').map(Number)
  const parsed = new Date(Date.UTC(year, month - 1, day))
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day
}

export function localDate(value: Date, timezone: string) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(value)
  const fields = Object.fromEntries(parts.map(part => [part.type, part.value]))
  return `${fields.year}-${fields.month}-${fields.day}`
}

export function dateFromSearch(search: string, fallback: string) {
  const value = new URLSearchParams(search).get('date') || ''
  return isCalendarDate(value) ? value : fallback
}

export function shiftDate(value: string, offset: number) {
  const [year, month, day] = value.split('-').map(Number)
  const shifted = new Date(Date.UTC(year, month - 1, day + offset))
  return shifted.toISOString().slice(0, 10)
}

/** Treat IANA aliases such as Asia/Calcutta and Asia/Kolkata as the same zone. */
export function timezonesEquivalent(left: string, right: string) {
  try {
    const canonical = (value: string) => new Intl.DateTimeFormat('en-US', { timeZone: value }).resolvedOptions().timeZone
    return canonical(left) === canonical(right)
  } catch {
    return left === right
  }
}
