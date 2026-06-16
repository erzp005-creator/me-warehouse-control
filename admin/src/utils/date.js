// Shared date-only formatter. Avoids the UTC-midnight off-by-one shift.
//
// `ship_by_date` / `order_date` arrive as date-only strings ('YYYY-MM-DD').
// `new Date('2026-06-09')` parses as UTC midnight per the ECMAScript spec, so
// `.toLocaleDateString()` in a negative-offset timezone (US Mountain/Pacific)
// renders the PREVIOUS calendar day (e.g. 6/8/2026). For a warehouse in MT/PT
// that makes today's orders look already-late and breaks the picking queue's
// ship-by sort.
//
// The fix is to never build a Date from the date-only string: split the
// 'YYYY-MM-DD' part and rebuild the label directly. This is the same approach
// the printed picking ticket already used (formerly PickingTicketPrint's local
// `formatUSDate`), now shared. Result is timezone-independent by construction.
//
// Accepts 'YYYY-MM-DD' or a full ISO timestamp (uses the leading date part).
// Returns 'MM/DD/YYYY', or '' for falsy / malformed input.
export function formatDateOnly(value) {
  if (!value) return '';
  const datePart = String(value).slice(0, 10);
  const [y, m, d] = datePart.split('-').map(Number);
  if (!y || !m || !d) return '';
  return `${String(m).padStart(2, '0')}/${String(d).padStart(2, '0')}/${y}`;
}
