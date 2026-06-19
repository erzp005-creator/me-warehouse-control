/**
 * useBatchedReceive: optimistic batched turbo-receive submitter.
 *
 * Mobile vitest has no React Native runtime (see components/__tests__/
 * ScanInput.test.js for the pattern), so the stateful hook is exercised two
 * ways: (1) real unit tests of the pure aggregator, and (2) a source-level
 * gate over the safety invariants that keep receipts from being lost or
 * double-counted. End-to-end turbo behavior is device-verified; the
 * server-side batch path is covered in api/tests/test_receiving.py.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, it, expect, beforeAll } from 'vitest';

import { aggregateReceiveBatch } from '../useBatchedReceive';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HOOK_PATH = resolve(__dirname, '..', 'useBatchedReceive.js');

const entry = (item_id, bin_id) => ({ key: item_id, payload: { item_id, bin_id } });

describe('aggregateReceiveBatch', () => {
  it('collapses repeated scans of one item into a single line with a count', () => {
    const items = aggregateReceiveBatch([entry(7, 3), entry(7, 3), entry(7, 3)]);
    expect(items).toEqual([{ item_id: 7, bin_id: 3, quantity: 3 }]);
  });

  it('keeps the same item in different bins as separate lines', () => {
    const items = aggregateReceiveBatch([entry(7, 3), entry(7, 4), entry(7, 3)]);
    expect(items).toEqual([
      { item_id: 7, bin_id: 3, quantity: 2 },
      { item_id: 7, bin_id: 4, quantity: 1 },
    ]);
  });

  it('groups distinct items and preserves first-seen order', () => {
    const items = aggregateReceiveBatch([entry(2, 1), entry(9, 1), entry(2, 1)]);
    expect(items).toEqual([
      { item_id: 2, bin_id: 1, quantity: 2 },
      { item_id: 9, bin_id: 1, quantity: 1 },
    ]);
  });

  it('returns an empty array for an empty buffer', () => {
    expect(aggregateReceiveBatch([])).toEqual([]);
  });

  it('conserves count: total quantity equals the number of scans', () => {
    const scans = [entry(1, 1), entry(1, 1), entry(2, 1), entry(2, 2), entry(3, 1)];
    const total = aggregateReceiveBatch(scans).reduce((s, it) => s + it.quantity, 0);
    expect(total).toBe(scans.length);
  });
});

describe('useBatchedReceive safety invariants (source gate)', () => {
  let src;
  beforeAll(() => { src = readFileSync(HOOK_PATH, 'utf8'); });

  it('flushes one batch at a time (single-flight guard)', () => {
    expect(src).toMatch(/if \(flushing\.current/);
  });

  it('flushes immediately once the buffer reaches batchMax', () => {
    expect(src).toMatch(/buffer\.current\.length >= batchMaxRef\.current/);
  });

  it('debounces flushing with a timer', () => {
    expect(src).toMatch(/setTimeout\(/);
  });

  it('drain only resolves when the buffer is empty and nothing is in flight', () => {
    expect(src).toMatch(/buffer\.current\.length === 0 && !flushing\.current/);
  });

  it('clears a batch\'s optimistic counts when it settles (success or failure)', () => {
    // bumpPending(key, -1) runs in the flush finally for every attempted scan
    expect(src).toMatch(/finally\s*\{[\s\S]*bumpPending\(key, -1\)/);
  });

  it('re-flushes scans that arrived during an in-flight flush', () => {
    expect(src).toMatch(/if \(buffer\.current\.length > 0\) flush\(\)/);
  });

  it('reset clears both pending and the buffer', () => {
    expect(src).toMatch(/pendingRef\.current = \{\}/);
    expect(src).toMatch(/buffer\.current = \[\]/);
  });
});
