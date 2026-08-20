/**
 * Fraud Review CSR notes stay on their own order.
 *
 * Reported from the warehouse channel: a note typed on the Fraud Review
 * screen does not stay on the order it was typed against. Push any order to
 * the queue and every note below it slides up onto the order above's row.
 *
 * Two defects combined. DataTable keyed rows by array index, and no admin
 * list payload carries a bare `id`, so `row.id || i` always fell through to
 * the index. MemoCell seeded its text with useState(initial), which runs on
 * mount only. Removing a row shifted every row below it up one index, React
 * reconciled by key, and each surviving MemoCell kept its old text while
 * being handed a different order's props. Nothing remounted, so nothing
 * reset.
 *
 * The damage is not cosmetic: the operator's natural next action is to
 * correct the note they are shown, which PATCHes /memo against the soId the
 * cell now holds, writing the note onto the wrong sales order. That endpoint
 * overwrites the memo column wholesale.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';

const apiGetMock = vi.fn();
const apiPatchMock = vi.fn();
const apiPostMock = vi.fn();
vi.mock('../api.js', () => ({
  api: {
    get: (...a) => apiGetMock(...a),
    patch: (...a) => apiPatchMock(...a),
    post: (...a) => apiPostMock(...a),
    put: vi.fn(), delete: vi.fn(),
  },
}));
vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1 }),
}));

import Fraud, { MemoCell } from '../pages/Fraud.jsx';

const json = (body, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300,
  status,
  json: () => Promise.resolve(body),
});

// Four flagged orders. The seeded memos matter: the visible symptom is
// existing notes sliding up a row, not just typed ones.
const ORDERS = [
  { so_id: 101, so_number: 'SO-101', customer_name: 'Ada Lovelace', memo: 'note one' },
  { so_id: 102, so_number: 'SO-102', customer_name: 'Grace Hopper', memo: 'note two' },
  { so_id: 103, so_number: 'SO-103', customer_name: 'Katherine Johnson', memo: '' },
  { so_id: 104, so_number: 'SO-104', customer_name: 'Margaret Hamilton', memo: '' },
];

// The memo textarea that shares a row with the given SO number, so an
// assertion names the order the operator is actually looking at rather than
// a position in the DOM.
function memoFor(soNumber) {
  const row = screen.getByText(soNumber).closest('tr');
  return row.querySelector('textarea');
}

async function renderQueue(orders = ORDERS) {
  apiGetMock.mockImplementation(() => json({ sales_orders: orders }));
  render(<Fraud />);
  await waitFor(() => expect(screen.getByText('SO-101')).toBeInTheDocument());
}

// Push the named order to the queue and let the list settle.
async function pushToQueue(soNumber) {
  apiPostMock.mockImplementation(() => json({ ok: true }));
  const row = screen.getByText(soNumber).closest('tr');
  await act(async () => {
    fireEvent.click(row.querySelector('button'));
  });
  await waitFor(() => expect(screen.queryByText(soNumber)).not.toBeInTheDocument());
}

describe('Fraud Review memo cells', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiPatchMock.mockReset();
    apiPostMock.mockReset();
  });

  it('keeps typed text on its own order when a row above is pushed to the queue', async () => {
    await renderQueue();

    fireEvent.change(memoFor('SO-103'), { target: { value: 'card mismatch, called customer' } });
    await pushToQueue('SO-101');

    // The whole point: the text is still next to the order it was written
    // for, and has not landed on its neighbour.
    expect(memoFor('SO-103')).toHaveValue('card mismatch, called customer');
    expect(memoFor('SO-104')).toHaveValue('');
  });

  it('keeps seeded notes on their own orders when the list shifts', async () => {
    await renderQueue();
    await pushToQueue('SO-101');

    expect(memoFor('SO-102')).toHaveValue('note two');
    expect(memoFor('SO-103')).toHaveValue('');
    expect(memoFor('SO-104')).toHaveValue('');
  });

  it('writes a correction to the order the operator is looking at', async () => {
    await renderQueue();

    fireEvent.change(memoFor('SO-103'), { target: { value: 'card mismatch' } });
    await pushToQueue('SO-101');

    // The operator edits what is on screen and tabs away. Before the fix the
    // cell showing this text was pointed at SO-104, so this PATCH landed on
    // 104 and overwrote whatever was there.
    apiPatchMock.mockImplementation(() => json({ so_id: 103, memo: 'card mismatch, refunded' }));
    const cell = memoFor('SO-103');
    fireEvent.change(cell, { target: { value: 'card mismatch, refunded' } });
    await act(async () => { fireEvent.blur(cell); });

    expect(apiPatchMock).toHaveBeenCalledTimes(1);
    expect(apiPatchMock).toHaveBeenCalledWith(
      '/admin/sales-orders/103/memo',
      { memo: 'card mismatch, refunded' },
    );
  });

  it('does not write anything just because the list changed', async () => {
    await renderQueue();
    await pushToQueue('SO-101');

    expect(apiPatchMock).not.toHaveBeenCalled();
  });

  it('re-seeds from props if the same cell is ever handed a different order', async () => {
    // Second lock, independent of row keys. MemoCell is rendered directly
    // with a changing soId, which is what the buggy reconciliation did to it.
    const { rerender } = render(<MemoCell soId={101} initial="note one" />);
    expect(screen.getByRole('textbox')).toHaveValue('note one');

    rerender(<MemoCell soId={104} initial="" />);
    expect(screen.getByRole('textbox')).toHaveValue('');
  });
});
