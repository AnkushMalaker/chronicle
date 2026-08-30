import { describe, expect, it } from 'vitest'
import { dateFromSearch, localDate, shiftDate, timezonesEquivalent } from './timelineNavigation'

describe('timeline date navigation', () => {
  it('uses a valid date from the public Timeline URL', () => {
    expect(dateFromSearch('?date=2026-02-19', '2026-08-28')).toBe('2026-02-19')
  })

  it('falls back to today when URL date state is malformed', () => {
    expect(dateFromSearch('?date=19-02-2026', '2026-08-28')).toBe('2026-08-28')
  })

  it('formats today in the stored timezone and shifts calendar dates safely', () => {
    const instant = new Date('2026-08-27T19:30:00.000Z')
    expect(localDate(instant, 'Asia/Calcutta')).toBe('2026-08-28')
    expect(localDate(instant, 'UTC')).toBe('2026-08-27')
    expect(shiftDate('2026-03-29', -1)).toBe('2026-03-28')
    expect(shiftDate('2026-03-29', 1)).toBe('2026-03-30')
  })

  it('recognizes canonical IANA timezone aliases', () => {
    expect(timezonesEquivalent('Asia/Kolkata', 'Asia/Calcutta')).toBe(true)
    expect(timezonesEquivalent('Asia/Kolkata', 'UTC')).toBe(false)
  })
})
