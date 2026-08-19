// Shared picking-ticket constants. Kept in a side-effect-free module so
// the list page can import the print-batch limit without pulling the
// print view's CSS into its bundle.

// Max tickets one Print All tab renders. Enforced on both sides: the
// list page slices its so_ids hand-off to this, and the print page
// re-clamps so a stale or hand-edited URL can't stack an unbounded run
// (and that many detail fetches) into one tab. When a queue exceeds
// this, the operator prints the top batch, those SOs get marked printed
// and drop off the Hide-Printed list, and the next Print All picks up
// where it left off.
export const PRINT_BATCH_LIMIT = 200;

// A "long order" has MORE THAN 4 line items -- i.e. 5+ distinct SKU rows
// on the SO. The Long Orders filter on the Picking Tickets page keeps only
// these so an operator can batch-print the orders that take the most
// picking effort. Counted as distinct sales_order_lines rows (line_count
// from the queue endpoint), not total quantity: one line of qty 10 is a
// short order.
export const LONG_ORDER_MIN_LINES = 5;

