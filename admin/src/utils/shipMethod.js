// Ship-method display reconciliation.
//
// `sales_orders.ship_method` records the customer's *requested* service and
// is never rewritten on ship. The `carrier` field, by contrast, is set to
// the carrier the order actually shipped on. The two diverge whenever
// carrier optimization, an operator override, or an external shipping-system routing rule
// switches a USPS-named service onto a UPS (1Z) label: the order then reads
// "Ship Method: USPS Ground Advantage" sitting next to a 1Z tracking number,
// with nothing reconciling them.
//
// This is display-only. We do not mutate ship_method or the outbound
// ship.confirmed service_level (that stays the requested service for
// marketplace confirmation); we just surface the authoritative carrier in
// the operator UI when it contradicts the method name.

const FEDEX = 'FEDEX';
const USPS = 'USPS';
const UPS = 'UPS';
const DHL = 'DHL';

// The carrier a free-text ship-method name implies, or null when the name
// claims no carrier ("Local Pickup", "Standard Shipping", "Expedited").
// Mirrors the carrier vocabulary on the Sentry side (carrier_from_ship_method
// + CarrierEngine.current_carrier).
export function carrierFromShipMethod(method) {
  const h = (method || '').toLowerCase();
  if (!h.trim()) return null;
  if (h.includes('fedex') || h.includes('fed ex')) return FEDEX;
  // USPS before UPS. 'usps' shares no substring with 'ups', but several USPS
  // services ("Ground Advantage", "Priority Mail", "First Class", "Media
  // Mail") never name the carrier outright, so match the service names too.
  if (
    h.includes('usps') ||
    h.includes('ground advantage') ||
    h.includes('priority') ||
    h.includes('first class') ||
    h.includes('first-class') ||
    h.includes('media')
  ) {
    return USPS;
  }
  if (h.includes('ups')) return UPS;
  if (h.includes('dhl')) return DHL;
  return null;
}

// Canonical carrier label from the (already authoritative) carrier field.
export function normalizeCarrier(carrier) {
  const c = (carrier || '').trim().toUpperCase();
  if (!c) return null;
  if (c.includes('FEDEX') || c.includes('FED EX')) return FEDEX;
  if (c.includes('USPS')) return USPS;
  if (c.includes('UPS')) return UPS;
  if (c.includes('DHL')) return DHL;
  return c; // 'OTHER', 'AMAZON', etc. pass through unchanged.
}

// Display string for a SO's ship method. Returns { text, diverged }.
// `diverged` is true only when the order has actually shipped (carrier
// present) on a carrier whose name the requested method contradicts; an
// unshipped order, a same-carrier ship, or a generic method name that
// claims no carrier all render the plain method text.
export function shipMethodDisplay(so, { fallback = '-' } = {}) {
  const method = (so && so.ship_method) || '';
  const actual = normalizeCarrier(so && so.carrier);
  const implied = carrierFromShipMethod(method);
  if (method && actual && implied && implied !== actual) {
    return { text: `${method} (shipped ${actual})`, diverged: true };
  }
  return { text: method || fallback, diverged: false };
}
