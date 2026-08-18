/**
 * Pure helpers behind the receive screen's bounded line list. Mobile vitest
 * has no React Native runtime, so the render path is device-verified; these
 * cover the logic that decides what renders -- ordering and paging -- without
 * a component mount. Same approach as hooks/__tests__/useBatchedReceive.test.js.
 */

import { describe, it, expect } from 'vitest';

import { orderReceiveLines, paginate } from '../receiveLines';

const line = (item_id, quantity_ordered, quantity_received) => ({
  item_id,
  po_line_id: item_id,
  quantity_ordered,
  quantity_received,
});

const ids = (rows) => rows.map((r) => r.line.item_id);

describe('orderReceiveLines', () => {
  it('folds optimistic pending into the received count', () => {
    const [row] = orderReceiveLines([line(1, 10, 3)], { 1: 2 });
    expect(row.received).toBe(5);
    expect(row.done).toBe(false);
    expect(row.hasPending).toBe(true);
  });

  it('treats no pending map as zero pending', () => {
    const [row] = orderReceiveLines([line(1, 10, 3)]);
    expect(row.received).toBe(3);
    expect(row.hasPending).toBe(false);
  });

  it('sinks fully-received lines below still-needed ones', () => {
    const rows = orderReceiveLines(
      [line(1, 5, 5), line(2, 5, 0), line(3, 5, 5), line(4, 5, 0)],
      {},
    );
    // not-done first (original order), then done (original order)
    expect(ids(rows)).toEqual([2, 4, 1, 3]);
  });

  it('keeps original order stable within a group regardless of engine sort', () => {
    const rows = orderReceiveLines(
      [line(10, 5, 0), line(20, 5, 0), line(30, 5, 0)],
      {},
    );
    expect(ids(rows)).toEqual([10, 20, 30]);
  });

  it('sinks a line that pending alone completes', () => {
    const rows = orderReceiveLines([line(1, 2, 1), line(2, 5, 0)], { 1: 1 });
    expect(rows[0].line.item_id).toBe(2); // still needed stays on top
    expect(rows[1].done).toBe(true); // item 1 completed by pending, sunk
  });

  it('counts an over-receipt as done', () => {
    const [row] = orderReceiveLines([line(1, 2, 5)], {});
    expect(row.done).toBe(true);
  });
});

describe('paginate', () => {
  const items = Array.from({ length: 10 }, (_, i) => i);

  it('returns the requested page slice with totals', () => {
    const r = paginate(items, 0, 4);
    expect(r.pageItems).toEqual([0, 1, 2, 3]);
    expect(r.totalPages).toBe(3);
    expect(r.safePage).toBe(0);
    expect(r.start).toBe(0);
  });

  it('slices the last (short) page', () => {
    const r = paginate(items, 2, 4);
    expect(r.pageItems).toEqual([8, 9]);
    expect(r.start).toBe(8);
  });

  it('clamps a page past the end down to the last page', () => {
    const r = paginate(items, 99, 4);
    expect(r.safePage).toBe(2);
    expect(r.pageItems).toEqual([8, 9]);
  });

  it('clamps a negative page up to zero', () => {
    const r = paginate(items, -3, 4);
    expect(r.safePage).toBe(0);
    expect(r.pageItems).toEqual([0, 1, 2, 3]);
  });

  it('reports one page for an empty list', () => {
    const r = paginate([], 0, 4);
    expect(r.totalPages).toBe(1);
    expect(r.safePage).toBe(0);
    expect(r.pageItems).toEqual([]);
  });
});
