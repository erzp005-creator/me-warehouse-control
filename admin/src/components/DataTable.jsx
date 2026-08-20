function sanitizeCsvValue(val) {
  if (typeof val !== 'string') return val ?? '';
  const escaped = `"${val.replace(/"/g, '""')}"`;
  if (/^[=+\-@\t\r]/.test(val)) return `"'${val.replace(/"/g, '""')}"`;
  return escaped;
}

// Columns that use `render` to display a React element (e.g. <StatusTag>)
// would otherwise serialize to "[object Object]" in the CSV. Prefer an
// explicit `csvValue(row)` when provided; otherwise fall back to the raw
// field value when `render` returned a React element.
function computeCellValue(col, row) {
  if (col.csvValue) return col.csvValue(row);
  if (col.render) {
    const rendered = col.render(row);
    if (rendered === null || rendered === undefined) return row[col.key];
    if (typeof rendered !== 'object') return rendered;
  }
  return row[col.key];
}

// An operator selecting text in a cell to copy an SO
// number or a SKU fired the row handler, which on some pages meant a
// navigation. Two things fix it, and both are here:
//
//   clickColumn  names the ONE column that opens the record, so the target
//                is discoverable and the rest of the row is free to select.
//                Opt-in: pages that do not set it keep whole-row clicking.
//   the guard    ignores any row click made while a selection exists, which
//                covers every page including the ones still using whole-row
//                clicking.
//
// A drag that ends inside a cell leaves a non-empty selection, so this
// distinguishes "finished selecting text" from "clicked".
function hasTextSelection() {
  if (typeof window === 'undefined' || !window.getSelection) return false;
  const sel = window.getSelection();
  return !!sel && !sel.isCollapsed && String(sel).trim().length > 0;
}

// Rows used to be keyed `row.id || i`. No admin list payload
// carries a bare `id`, so every table in the admin fell through to the array
// index and React reconciled rows by position. A row removed from the middle
// of the list shifted every row below it up one index, and any cell holding
// state was handed a different record's props without remounting. Fraud
// Review's memo box was where that surfaced: the CSR's typed note stayed put
// on screen while the order under it changed, so an edit-in-place wrote the
// note onto the wrong sales order.
//
// `rowKey` names what identifies a row: a field name, or a function for the
// tables whose rows have no single id column.
//
// Returns undefined when the row cannot be identified, so the caller can tell
// a real key apart from the index it substitutes. Resolving straight to the
// index here would hide a rowKey that silently misses on some rows.
function resolveRowKey(rowKey, row) {
  if (typeof rowKey === 'function') return rowKey(row);
  if (typeof rowKey === 'string') {
    const value = row?.[rowKey];
    if (value !== undefined && value !== null) return value;
  }
  return undefined;
}

// A wrong rowKey is worse than no rowKey: duplicate keys make React drop or
// merge rows outright, where an index at least renders. Both guards below are
// dev-only, so they cost nothing in the built bundle, and they fire in the
// test run -- which is the point, since that is where a bad key on a page
// nobody opened this week gets caught.
function warnOnBadKeys(resolved, rowKey, columns) {
  if (!import.meta.env?.DEV) return;
  const where = columns?.map((c) => c.label).filter(Boolean).join(', ');
  if (rowKey === undefined) {
    console.warn(`DataTable: no rowKey given, falling back to array index. Columns: ${where}`);
    return;
  }
  if (resolved.some((k) => k === undefined)) {
    const named = typeof rowKey === 'string' ? `"${rowKey}"` : 'function';
    console.warn(`DataTable: rowKey ${named} did not resolve on every row. Columns: ${where}`);
  }
  const present = resolved.filter((k) => k !== undefined);
  if (new Set(present).size !== present.length) {
    console.warn(`DataTable: rowKey produced duplicate keys. Columns: ${where}`);
  }
}

export default function DataTable({
  columns,
  data,
  rowKey,
  pagination,
  onPageChange,
  onRowClick,
  clickColumn,
  emptyMessage = 'No records found',
  sortKey,
  sortDir,
  onSort,
}) {
  function exportCSV() {
    if (!data || data.length === 0) return;
    const headers = columns.map((c) => c.label);
    const rows = data.map((row) =>
      columns.map((c) => sanitizeCsvValue(computeCellValue(c, row)))
    );
    const csv = [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${new Date().toISOString().slice(0, 10)}InvExport.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const resolvedKeys = (data || []).map((row) => resolveRowKey(rowKey, row));
  if (data && data.length > 0) warnOnBadKeys(resolvedKeys, rowKey, columns);

  return (
    <div className="data-table-wrapper">
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((col) => {
              const isSortable = col.sortable && onSort;
              const isActive = sortKey === col.key;
              return (
                <th
                  key={col.key || col.label}
                  style={isSortable ? { cursor: 'pointer', userSelect: 'none' } : undefined}
                  onClick={isSortable ? () => onSort(col.key) : undefined}
                >
                  {col.label}
                  {isActive && (
                    <span style={{ marginLeft: 4, fontSize: 10 }}>
                      {sortDir === 'asc' ? '\u25B2' : '\u25BC'}
                    </span>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {(!data || data.length === 0) ? (
            <tr>
              <td colSpan={columns.length} className="table-empty">
                {emptyMessage}
              </td>
            </tr>
          ) : (
            data.map((row, i) => {
              // With clickColumn set the row itself is inert and the named
              // cell carries the handler; without it the whole row stays
              // clickable, as every page behaved before.
              const cellIsTarget = (col) =>
                clickColumn && (col.key || col.label) === clickColumn;
              const activate = () => {
                if (hasTextSelection()) return;
                onRowClick?.(row);
              };
              return (
                <tr
                  key={resolvedKeys[i] ?? i}
                  className={onRowClick && !clickColumn ? 'clickable' : ''}
                  onClick={onRowClick && !clickColumn ? activate : undefined}
                >
                  {columns.map((col) => (
                    <td
                      key={col.key || col.label}
                      className={[
                        col.mono ? 'mono' : '',
                        onRowClick && cellIsTarget(col) ? 'cell-clickable' : '',
                      ].filter(Boolean).join(' ')}
                      onClick={onRowClick && cellIsTarget(col) ? activate : undefined}
                    >
                      {col.render ? col.render(row) : row[col.key]}
                    </td>
                  ))}
                </tr>
              );
            })
          )}
        </tbody>
      </table>
      {pagination && (
        <div className="pagination">
          <span>
            Page {pagination.page} of {pagination.pages} ({pagination.total} total)
          </span>
          <div className="pagination-buttons">
            <button className="btn-sm btn" onClick={exportCSV} style={{ marginRight: 8 }}>
              Export CSV
            </button>
            <button
              className="pagination-btn"
              disabled={pagination.page <= 1}
              onClick={() => onPageChange(pagination.page - 1)}
            >
              Prev
            </button>
            <button
              className="pagination-btn"
              disabled={pagination.page >= pagination.pages}
              onClick={() => onPageChange(pagination.page + 1)}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
