// Multi-Orders grouping for the Picking Tickets queue. Two orders that
// ship to the same address can go in one box to save on postage, so this
// module derives a normalized "ships-together" key from an order's
// shipping address and groups the queue by it.
//
// Kept side-effect-free (like pickingConstants.js) so BOTH the list page
// and the standalone print-all page can import the exact same key logic.
// If these two ever computed the key differently, the on-screen groups
// and the printed "SHIP WITH" banners would disagree -- which is the one
// bug this shared module exists to prevent.

// Lowercase, collapse internal whitespace, trim, and drop trailing
// punctuation ("St." -> "st"). Enough to fold the common human/marketplace
// variants of the same address ("12 Elm St" vs "12 elm st.") without
// pretending to be a real address canonicalizer.
function norm(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[.,#]+$/, '')
    .trim();
}

// US zips arrive as "80112" or "80112-1234"; group on the 5-digit prefix
// so ZIP+4 vs ZIP-5 for the same door still match. Non-US / free-form
// postals fall through to a plain normalize.
function zip5(postal) {
  const p = norm(postal);
  const m = p.match(/^(\d{5})/);
  return m ? m[1] : p;
}

// Derive the ships-together key for one order, or null if the order has
// no usable address (never group a null key -- ungroupable orders stay
// singletons and are hidden when the Multi-Orders filter is on).
//
// Prefer the structured shipping_address_* fields. When line1 is absent
// (e.g. a marketplace SO not yet backfilled that only carries the legacy
// single-string ship_address) fall back to the normalized legacy string
// so those still group among themselves. line1 + a postal are the minimum
// signal we trust; anything short of that is ungroupable.
// The recipient name is part of the key: one street address can host many
// distinct customers (roommates, offices, freight forwarders whose suite
// code rides in the name line), and combining two customers' orders into
// one labelled shipment is far worse than missing a postage combine. Name
// variants of one person ("B. Bird" vs "Bruce Bird") therefore split --
// that is the safe direction.
export function shippingGroupKey(order) {
  if (!order) return null;
  const name = norm(order.shipping_address_name || order.customer_name);
  const line1 = norm(order.shipping_address_line1);
  if (line1) {
    const postal = zip5(order.shipping_address_postal_code);
    if (!postal) return null;
    const parts = [
      name,
      line1,
      norm(order.shipping_address_line2),
      norm(order.shipping_address_city),
      norm(order.shipping_address_state),
      postal,
    ];
    return parts.join('|');
  }
  const legacy = norm(order.ship_address);
  return legacy ? `legacy:${name}|${legacy}` : null;
}

// Group orders by ships-together key, keeping only groups of 2+ (a lone
// order is not a "multi-order"). Returns an array of groups in a stable
// order: each group's members preserve their incoming order, and the
// groups themselves are ordered by their earliest ship_by_date so the
// most-urgent combined shipments rise to the top of the print stack.
// Orders with a null key are dropped (ungroupable).
export function groupOrdersByAddress(orders) {
  const byKey = new Map();
  for (const order of orders || []) {
    const key = shippingGroupKey(order);
    if (!key) continue;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(order);
  }

  const groups = [];
  for (const [key, members] of byKey) {
    if (members.length < 2) continue;
    groups.push({ key, orders: members });
  }

  // Earliest ship_by_date within the group is the group's urgency. Nulls
  // sort last so dateless groups fall to the bottom rather than the top.
  const earliest = (group) => {
    let best = null;
    for (const o of group.orders) {
      if (!o.ship_by_date) continue;
      const t = new Date(o.ship_by_date).getTime();
      if (best === null || t < best) best = t;
    }
    return best;
  };
  groups.sort((a, b) => {
    const ea = earliest(a);
    const eb = earliest(b);
    if (ea === null && eb === null) return 0;
    if (ea === null) return 1;
    if (eb === null) return -1;
    return ea - eb;
  });
  return groups;
}
