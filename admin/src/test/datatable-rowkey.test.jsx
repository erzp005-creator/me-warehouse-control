/**
 * DataTable row identity.
 *
 * Rows used to be keyed `row.id || i`. No admin list payload carries a bare
 * `id`, so every table in the admin fell through to the array index and React
 * reconciled rows by position rather than by record. A row removed from the
 * middle of the list handed every cell below it a different record's props
 * without remounting.
 *
 * Two halves are pinned here:
 *   behaviour  a stateful cell keeps its state with its own record across a
 *              removal, and only when rowKey is supplied
 *   coverage   every DataTable call site in the app supplies rowKey, so the
 *              next page added cannot quietly reintroduce index keying
 *
 * The dev-only warnings are pinned too. A wrong rowKey is worse than none:
 * duplicate keys make React drop or merge rows, where an index at least
 * renders every row.
 */

import React, { useState } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import DataTable from '../components/DataTable.jsx';

// Stands in for Fraud Review's memo box: the only kind of cell that can tell
// the difference between keying by record and keying by position.
function StatefulCell({ seed }) {
  const [value, setValue] = useState(seed);
  return <input value={value} onChange={(e) => setValue(e.target.value)} />;
}

const COLUMNS = [
  { key: 'so_number', label: 'SO Number' },
  { key: 'memo', label: 'Memo', render: (r) => <StatefulCell seed={r.memo || ''} /> },
];
const ROWS = [
  { so_id: 1, so_number: 'SO-1', memo: '' },
  { so_id: 2, so_number: 'SO-2', memo: '' },
  { so_id: 3, so_number: 'SO-3', memo: '' },
];

function inputFor(soNumber) {
  return screen.getByText(soNumber).closest('tr').querySelector('input');
}

describe('DataTable rowKey', () => {
  describe('keying behaviour', () => {
    it('keeps cell state with its own record when a row above is removed', () => {
      const { rerender } = render(
        <DataTable columns={COLUMNS} data={ROWS} rowKey="so_id" />
      );

      fireEvent.change(inputFor('SO-3'), { target: { value: 'typed on 3' } });
      rerender(<DataTable columns={COLUMNS} data={ROWS.slice(1)} rowKey="so_id" />);

      expect(inputFor('SO-3')).toHaveValue('typed on 3');
      expect(inputFor('SO-2')).toHaveValue('');
    });

    it('moves cell state onto the wrong record without rowKey', () => {
      // The defect itself, pinned so the index fallback is understood rather
      // than assumed harmless. Text typed against SO-2 sits at index 1;
      // dropping SO-1 puts SO-3 at index 1, and the text goes with the
      // position rather than the order. This is the "every note below slides
      // up onto the order above's row" the issue describes.
      const { rerender } = render(<DataTable columns={COLUMNS} data={ROWS} />);

      fireEvent.change(inputFor('SO-2'), { target: { value: 'typed on 2' } });
      rerender(<DataTable columns={COLUMNS} data={ROWS.slice(1)} />);

      expect(inputFor('SO-3')).toHaveValue('typed on 2');
      expect(inputFor('SO-2')).toHaveValue('');
    });

    it('accepts a function for rows with no single id column', () => {
      const lots = [
        { bin_id: 7, lot: 'A', label: 'row-A' },
        { bin_id: 7, lot: 'B', label: 'row-B' },
      ];
      const cols = [
        { key: 'label', label: 'Label' },
        { key: 'memo', label: 'Memo', render: (r) => <StatefulCell seed={r.lot} /> },
      ];
      const key = (r) => `${r.bin_id}-${r.lot}`;

      const { rerender } = render(<DataTable columns={cols} data={lots} rowKey={key} />);
      fireEvent.change(inputFor('row-B'), { target: { value: 'edited B' } });
      rerender(<DataTable columns={cols} data={lots.slice(1)} rowKey={key} />);

      expect(inputFor('row-B')).toHaveValue('edited B');
    });
  });

  describe('dev-time guards', () => {
    let warn;
    beforeEach(() => { warn = vi.spyOn(console, 'warn').mockImplementation(() => {}); });
    afterEach(() => { warn.mockRestore(); });

    const messages = () => warn.mock.calls.map((c) => String(c[0])).join('\n');

    it('warns when no rowKey is given', () => {
      render(<DataTable columns={COLUMNS} data={ROWS} />);
      expect(messages()).toMatch(/no rowKey given/);
    });

    it('warns when the named field is missing on a row', () => {
      const partial = [{ so_id: 1, so_number: 'SO-1' }, { so_number: 'SO-2' }];
      render(<DataTable columns={COLUMNS} data={partial} rowKey="so_id" />);
      expect(messages()).toMatch(/did not resolve on every row/);
    });

    it('warns when the key is not unique', () => {
      // Two lots of one item in one bin: distinct inventory rows, identical
      // bin_id. This is why the nested inventory grids key on inventory_id.
      const dupes = [{ bin_id: 7, so_number: 'A' }, { bin_id: 7, so_number: 'B' }];
      render(<DataTable columns={COLUMNS} data={dupes} rowKey="bin_id" />);
      expect(messages()).toMatch(/duplicate keys/);
    });

    it('stays quiet on a well-formed table', () => {
      render(<DataTable columns={COLUMNS} data={ROWS} rowKey="so_id" />);
      expect(warn).not.toHaveBeenCalled();
    });

    it('stays quiet on an empty table', () => {
      render(<DataTable columns={COLUMNS} data={[]} />);
      expect(warn).not.toHaveBeenCalled();
    });
  });

  describe('call-site coverage', () => {
    // Walks the opening tag brace-aware: props hold arrow functions, so the
    // first '>' in the source is usually part of '=>' rather than the end of
    // the tag.
    function openingTags(source) {
      const tags = [];
      let at = source.indexOf('<DataTable');
      while (at !== -1) {
        let depth = 0;
        let i = at + '<DataTable'.length;
        for (; i < source.length; i += 1) {
          const c = source[i];
          if (c === '{') depth += 1;
          else if (c === '}') depth -= 1;
          else if (c === '>' && depth === 0) break;
        }
        tags.push(source.slice(at, i + 1));
        at = source.indexOf('<DataTable', i);
      }
      return tags;
    }

    function jsxFiles(dir) {
      return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const full = join(dir, entry.name);
        if (entry.isDirectory()) return entry.name === 'test' ? [] : jsxFiles(full);
        return entry.name.endsWith('.jsx') ? [full] : [];
      });
    }

    it('every call site in the app supplies rowKey', () => {
      const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
      const missing = [];
      let total = 0;

      for (const file of jsxFiles(root)) {
        const source = readFileSync(file, 'utf8');
        if (!source.includes('<DataTable')) continue;
        for (const tag of openingTags(source)) {
          total += 1;
          if (!/\browKey=/.test(tag)) {
            missing.push(`${file.replace(root, 'src')}: ${tag.split('\n')[0].trim()}`);
          }
        }
      }

      expect(total).toBeGreaterThan(0);
      expect(missing, `DataTable call sites without rowKey:\n${missing.join('\n')}`).toEqual([]);
    });
  });
});
