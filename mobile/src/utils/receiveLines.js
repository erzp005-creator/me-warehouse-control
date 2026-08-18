/**
 * Pure helpers for the receive screen's PO line list.
 *
 * Extracted from ReceiveScreen so they can be useMemo'd (recompute only when
 * lines or pending counts change, not on every keystroke) and unit-tested
 * without a React Native runtime -- same pattern as aggregateReceiveBatch in
 * hooks/useBatchedReceive.js.
 */

/**
 * Annotate each PO line with its effective received count (server-confirmed
 * plus optimistic pending) and sort so still-needed lines stay at the top
 * while fully-received lines sink to the bottom. Stable within each group:
 * the original line order is preserved via an explicit index tiebreak, so the
 * result does not depend on the JS engine's sort stability.
 */
export function orderReceiveLines(lines, pendingByItem = {}) {
  return lines
    .map((line, index) => {
      const pending = pendingByItem[line.item_id] || 0;
      const received = line.quantity_received + pending;
      return {
        line,
        index,
        received,
        done: received >= line.quantity_ordered,
        hasPending: pending > 0,
      };
    })
    .sort((a, b) => (a.done - b.done) || (a.index - b.index));
}

/**
 * Bound how many rows render at once. A 700-line PO in a React Native
 * ScrollView mounts every row and re-mounts them on each scan-driven render,
 * which is the receiving slowdown on large POs. Slicing to a fixed page keeps
 * the rendered row count constant regardless of PO size. `page` is clamped so
 * a reorder (a line completing and sinking past a page boundary) can never
 * strand the view on an out-of-range page.
 */
export function paginate(items, page, pageSize) {
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const safePage = Math.min(Math.max(0, page), totalPages - 1);
  const start = safePage * pageSize;
  return {
    pageItems: items.slice(start, start + pageSize),
    totalPages,
    safePage,
    start,
  };
}
