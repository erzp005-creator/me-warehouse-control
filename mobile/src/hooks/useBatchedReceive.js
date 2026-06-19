import { useRef, useState, useCallback, useEffect } from 'react';

/**
 * Collapse buffered scans into one receive line per (item_id, bin_id), with
 * quantity = number of scans. First-seen (item_id, bin_id) order is kept.
 * Exported for unit testing.
 */
export function aggregateReceiveBatch(entries) {
  const byKey = new Map();
  for (const { payload } of entries) {
    const k = `${payload.item_id}:${payload.bin_id}`;
    const cur = byKey.get(k) || { item_id: payload.item_id, bin_id: payload.bin_id, quantity: 0 };
    cur.quantity += 1;
    byKey.set(k, cur);
  }
  return [...byKey.values()];
}

/**
 * Optimistic batched submitter for turbo receiving.
 *
 * Decouples the operator's scan rate from backend latency: each scan
 * counts instantly (optimistic `pending`) and is buffered, then the buffer
 * is flushed to the backend in batches -- debounced by `flushMs`, or
 * immediately once it reaches `batchMax`. Only one flush is in flight at a
 * time; scans that arrive during a flush ride the next batch.
 *
 * - enqueue(key, payload): count +1 for `key` and buffer `payload`.
 * - getPending(key): synchronous current pending for `key` (ref-backed, so
 *   rapid same-tick scans project correctly before React re-renders).
 * - onConfirm(items, resp): a batch committed server-side (reconcile up).
 * - onError(items, err): a batch failed; `pending` is rolled back -- the
 *   caller should surface it and refetch server truth.
 * - drain(): flush everything and resolve once the buffer is empty and no
 *   batch is in flight. Call before leaving a PO so nothing is lost.
 * - reset(): drop buffered/pending state (e.g. switching POs after a drain).
 *
 * `pending[key]` is decremented when a batch settles, success or failure --
 * on success the caller folds the quantities into its confirmed counts, on
 * failure the optimistic counts simply disappear.
 *
 * Callbacks and config are held in refs, so the caller may pass fresh
 * closures every render without breaking the in-flight flush loop.
 */
export default function useBatchedReceive({ submit, onConfirm, onError, flushMs = 400, batchMax = 20 }) {
  const submitRef = useRef(submit); submitRef.current = submit;
  const onConfirmRef = useRef(onConfirm); onConfirmRef.current = onConfirm;
  const onErrorRef = useRef(onError); onErrorRef.current = onError;
  const flushMsRef = useRef(flushMs); flushMsRef.current = flushMs;
  const batchMaxRef = useRef(batchMax); batchMaxRef.current = batchMax;

  const buffer = useRef([]);          // [{ key, payload }] not yet flushed
  const flushing = useRef(false);
  const timer = useRef(null);
  const drainWaiters = useRef([]);
  const pendingRef = useRef({});      // synchronous mirror of `pending`
  const [pending, setPending] = useState({});
  const [busy, setBusy] = useState(false);

  const bumpPending = (key, delta) => {
    const next = (pendingRef.current[key] || 0) + delta;
    if (next <= 0) delete pendingRef.current[key];
    else pendingRef.current[key] = next;
    setPending({ ...pendingRef.current });
  };

  const getPending = useCallback((key) => pendingRef.current[key] || 0, []);

  const maybeSettleDrain = () => {
    if (buffer.current.length === 0 && !flushing.current) {
      setBusy(false);
      const waiters = drainWaiters.current;
      drainWaiters.current = [];
      waiters.forEach((w) => w());
    }
  };

  const flush = useCallback(async () => {
    if (flushing.current || buffer.current.length === 0) {
      maybeSettleDrain();
      return;
    }
    flushing.current = true;
    setBusy(true);
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }

    const batch = buffer.current;
    buffer.current = [];

    const items = aggregateReceiveBatch(batch);

    try {
      const resp = await submitRef.current(items);
      onConfirmRef.current?.(items, resp);
    } catch (err) {
      onErrorRef.current?.(items, err);
    } finally {
      // The batch has settled either way: clear its optimistic counts.
      for (const { key } of batch) bumpPending(key, -1);
      flushing.current = false;
      if (buffer.current.length > 0) flush();
      else maybeSettleDrain();
    }
  }, []);

  const scheduleFlush = useCallback(() => {
    if (buffer.current.length >= batchMaxRef.current) { flush(); return; }
    if (timer.current) return;
    timer.current = setTimeout(() => { timer.current = null; flush(); }, flushMsRef.current);
  }, [flush]);

  const enqueue = useCallback((key, payload) => {
    buffer.current.push({ key, payload });
    bumpPending(key, +1);
    setBusy(true);
    scheduleFlush();
  }, [scheduleFlush]);

  const drain = useCallback(() => new Promise((resolve) => {
    if (buffer.current.length === 0 && !flushing.current) { resolve(); return; }
    drainWaiters.current.push(resolve);
    flush();
  }), [flush]);

  const reset = useCallback(() => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    buffer.current = [];
    pendingRef.current = {};
    setPending({});
    setBusy(false);
  }, []);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const pendingTotal = Object.values(pending).reduce((a, b) => a + b, 0);

  return { enqueue, getPending, pending, pendingTotal, busy, drain, reset };
}
