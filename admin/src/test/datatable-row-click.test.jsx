/**
 * DataTable row-click targeting (item 4).
 *
 *   "I don't need the order to open when I try to copy something from the
 *    backorder tab."
 *
 * Whole-row onClick meant selecting an SO number or a SKU to copy it fired
 * the row handler, which on the backorder queue meant a navigation.
 *
 * Two mechanisms, both tested here:
 *   clickColumn  names the single cell that opens the record. The rest of
 *                the row is inert and free to select. Opt-in, so the nine
 *                pages nobody complained about keep whole-row clicking.
 *   the guard    ignores a row click made while text is selected, which
 *                covers the pages still using whole-row clicking.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import DataTable from '../components/DataTable.jsx';

const COLUMNS = [
  { key: 'so_number', label: 'SO Number', mono: true },
  { key: 'customer_name', label: 'Customer' },
];
const DATA = [
  { so_number: 'SO-1', customer_name: 'Ada Lovelace' },
  { so_number: 'SO-2', customer_name: 'Grace Hopper' },
];

// jsdom has no real selection model, so drive window.getSelection directly.
function setSelection(text) {
  window.getSelection = () => ({
    isCollapsed: text === '',
    toString: () => text,
  });
}
function clearSelection() {
  window.getSelection = () => ({ isCollapsed: true, toString: () => '' });
}

describe('DataTable row click (item 4)', () => {
  beforeEach(() => { clearSelection(); });
  afterEach(() => { delete window.getSelection; });

  describe('with clickColumn set', () => {
    it('opens from the named cell only', () => {
      const onRowClick = vi.fn();
      render(<DataTable columns={COLUMNS} data={DATA}
        onRowClick={onRowClick} clickColumn="so_number" />);

      fireEvent.click(screen.getByText('SO-1'));
      expect(onRowClick).toHaveBeenCalledTimes(1);
      expect(onRowClick).toHaveBeenCalledWith(DATA[0]);
    });

    it('ignores clicks on every other cell', () => {
      const onRowClick = vi.fn();
      render(<DataTable columns={COLUMNS} data={DATA}
        onRowClick={onRowClick} clickColumn="so_number" />);

      fireEvent.click(screen.getByText('Ada Lovelace'));
      expect(onRowClick).not.toHaveBeenCalled();
    });

    it('marks the target cell so the affordance is discoverable', () => {
      render(<DataTable columns={COLUMNS} data={DATA}
        onRowClick={vi.fn()} clickColumn="so_number" />);

      expect(screen.getByText('SO-1').className).toContain('cell-clickable');
      expect(screen.getByText('Ada Lovelace').className).not.toContain('cell-clickable');
    });

    it('does not mark the whole row clickable', () => {
      const { container } = render(<DataTable columns={COLUMNS} data={DATA}
        onRowClick={vi.fn()} clickColumn="so_number" />);
      expect(container.querySelectorAll('tr.clickable').length).toBe(0);
    });

    it('still ignores the target cell while text is selected', () => {
      const onRowClick = vi.fn();
      render(<DataTable columns={COLUMNS} data={DATA}
        onRowClick={onRowClick} clickColumn="so_number" />);

      setSelection('SO-1');
      fireEvent.click(screen.getByText('SO-1'));
      expect(onRowClick).not.toHaveBeenCalled();
    });
  });

  describe('without clickColumn (the other nine pages)', () => {
    it('keeps whole-row clicking', () => {
      const onRowClick = vi.fn();
      render(<DataTable columns={COLUMNS} data={DATA} onRowClick={onRowClick} />);

      fireEvent.click(screen.getByText('Ada Lovelace'));
      expect(onRowClick).toHaveBeenCalledWith(DATA[0]);
    });

    it('marks rows clickable as before', () => {
      const { container } = render(
        <DataTable columns={COLUMNS} data={DATA} onRowClick={vi.fn()} />);
      expect(container.querySelectorAll('tr.clickable').length).toBe(2);
    });

    it('ignores the click when the operator has selected text', () => {
      const onRowClick = vi.fn();
      render(<DataTable columns={COLUMNS} data={DATA} onRowClick={onRowClick} />);

      setSelection('SO-1');
      fireEvent.click(screen.getByText('SO-1'));
      expect(onRowClick).not.toHaveBeenCalled();
    });

    it('a whitespace-only selection does not block the click', () => {
      // A collapsed-but-not-empty selection is an artefact, not an intent
      // to copy; treating it as one would make rows feel dead.
      const onRowClick = vi.fn();
      render(<DataTable columns={COLUMNS} data={DATA} onRowClick={onRowClick} />);

      setSelection('   ');
      fireEvent.click(screen.getByText('SO-1'));
      expect(onRowClick).toHaveBeenCalledTimes(1);
    });
  });

  it('renders no click affordance at all without onRowClick', () => {
    const { container } = render(<DataTable columns={COLUMNS} data={DATA} />);
    expect(container.querySelectorAll('tr.clickable').length).toBe(0);
    expect(container.querySelectorAll('td.cell-clickable').length).toBe(0);
  });
});
