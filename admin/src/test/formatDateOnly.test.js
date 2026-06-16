import { describe, it, expect } from 'vitest';
import { formatDateOnly } from '../utils/date.js';

// sentry-wms#52: a date-only string must format to the same calendar day in
// any timezone. The helper avoids `new Date()` entirely, so this holds even
// under a negative-offset TZ (e.g. run with TZ='America/Denver').
describe('formatDateOnly', () => {
  it('formats a date-only string to the same calendar day (no UTC-midnight shift)', () => {
    expect(formatDateOnly('2026-06-09')).toBe('06/09/2026');
  });

  it('uses the date part of a full ISO timestamp', () => {
    expect(formatDateOnly('2026-06-09T23:30:00Z')).toBe('06/09/2026');
  });

  it('zero-pads single-digit month and day', () => {
    expect(formatDateOnly('2026-01-05')).toBe('01/05/2026');
  });

  it('returns empty string for falsy or malformed input', () => {
    expect(formatDateOnly('')).toBe('');
    expect(formatDateOnly(null)).toBe('');
    expect(formatDateOnly(undefined)).toBe('');
    expect(formatDateOnly('not-a-date')).toBe('');
  });
});
