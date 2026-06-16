import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import { formatDateOnly } from '../utils/date.js';
import { useAuth } from '../auth.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

const STATUS_OPTIONS = ['All', 'OPEN', 'PICKED', 'PACKED', 'SHIPPED', 'CANCELLED'];
const EDITABLE_STATUS_OPTIONS = ['OPEN', 'PICKED', 'PACKED', 'SHIPPED', 'CANCELLED'];
// Lines are terminal once the SO has shipped or cancelled. Header edits
// remain ADMIN-only for SHIPPED (external-system backfill); for CANCELLED
// the operator should reopen via the cancel-undo workflow, not edit.
const LINE_TERMINAL_STATUSES = new Set(['SHIPPED', 'CANCELLED']);
// so-refinement: forward-flow ordering. Mirrors _STATUS_ORDER in
// api/services/sales_order_service.py. PICKING / PACKING / ALLOCATED
// were retired in v1.13.0 (mig 058); the live flow is
// OPEN -> PICKED -> PACKED -> SHIPPED. Backward transitions from
// PICKED/PACKED/SHIPPED require the revert-status flow so pick / pack /
// ship side effects are unwound.
const STATUS_ORDER = {
  OPEN: 0, PICKED: 1, PACKED: 2, SHIPPED: 3,
};
const REVERTABLE_STATUSES = new Set(['PICKED', 'PACKED', 'SHIPPED']);

// Matches PurchaseOrders.formatApiError: surfaces field-level details
// from the @validate_body decorator instead of the bare "validation_error".
function formatApiError(data, fallback) {
  if (!data) return fallback;
  if (Array.isArray(data.details) && data.details.length > 0) {
    return data.details
      .map((d) => {
        const field = Array.isArray(d.loc) && d.loc.length ? d.loc.join('.') : 'request';
        return `${field}: ${d.msg}`;
      })
      .join('; ');
  }
  return data.error || fallback;
}

// v1.8.0 (#268) per-component billing/shipping address fields. Order
// must match the canonical column ordering for round-trip consistency.
const ADDRESS_FIELD_KEYS = [
  'billing_address_name', 'billing_address_line1', 'billing_address_line2',
  'billing_address_city', 'billing_address_state',
  'billing_address_postal_code', 'billing_address_country',
  'billing_address_phone',
  'shipping_address_name', 'shipping_address_line1', 'shipping_address_line2',
  'shipping_address_city', 'shipping_address_state',
  'shipping_address_postal_code', 'shipping_address_country',
  'shipping_address_phone',
];

const ADDRESS_FIELD_LABELS = {
  billing_address_name: 'Name',
  billing_address_line1: 'Line 1',
  billing_address_line2: 'Line 2',
  billing_address_city: 'City',
  billing_address_state: 'State / Region',
  billing_address_postal_code: 'Postal Code',
  billing_address_country: 'Country',
  billing_address_phone: 'Phone',
  shipping_address_name: 'Name',
  shipping_address_line1: 'Line 1',
  shipping_address_line2: 'Line 2',
  shipping_address_city: 'City',
  shipping_address_state: 'State / Region',
  shipping_address_postal_code: 'Postal Code',
  shipping_address_country: 'Country',
  shipping_address_phone: 'Phone',
};

function NullableValue({ value }) {
  if (value === null || value === undefined || value === '') {
    return <span style={{ color: 'var(--text-secondary)' }}>-</span>;
  }
  return <span>{value}</span>;
}

export default function SalesOrders() {
  const { user } = useAuth();
  const isAdmin = user?.role === 'ADMIN';
  const hasSOFullEdit = isAdmin || (user?.allowed_overrides || []).includes('so-full-edit');

  const [searchParams] = useSearchParams();
  const [search, setSearch] = useState(searchParams.get('q') || '');
  const [orders, setOrders] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('All');
  const [selectedSO, setSelectedSO] = useState(null);
  const [soLines, setSOLines] = useState([]);
  const [editing, setEditing] = useState(null);
  const [editForm, setEditForm] = useState({});
  const [editError, setEditError] = useState('');
  const [confirmCancel, setConfirmCancel] = useState(false);
  // Admin virtual-pick sub-modal: per-line list of {bin_id, quantity}
  // entries. Two entries on the same line model a split-bin pick.
  // adminPickBinsByLine caches the GET /available-bins response per
  // so_line_id so the dropdowns render without a fetch on every keystroke.
  const [adminPicking, setAdminPicking] = useState(null);
  const [adminPickForm, setAdminPickForm] = useState({});
  const [adminPickBinsByLine, setAdminPickBinsByLine] = useState({});
  const [adminPickError, setAdminPickError] = useState('');
  const [adminPickSubmitting, setAdminPickSubmitting] = useState(false);
  // so-refinement: backward status transition confirmation modal.
  // Carries { newStatus, pickTasks, keepIds, error, busy, mode }.
  // Checked rows (in keepIds) stay PICKED; unchecked rows release.
  // Address fields moved inline into editForm; the separate
  // address modal is retired.
  const [revertConfirm, setRevertConfirm] = useState(null);
  // SO line CRUD inside the edit modal (mig 062). State
  // mirrors PurchaseOrders.jsx: editLines is the working copy, optimistic
  // PATCH/POST/DELETE update it without refetching; lineErrors keep
  // the inline rejection visible per row.
  const [editLines, setEditLines] = useState([]);
  const [lineErrors, setLineErrors] = useState({});
  const [newLineSku, setNewLineSku] = useState('');
  const [newLineQty, setNewLineQty] = useState('');
  const [newLineError, setNewLineError] = useState('');
  const [addingLine, setAddingLine] = useState(false);
  const [skuSuggestions, setSkuSuggestions] = useState([]);
  const [resolvedItem, setResolvedItem] = useState(null);
  // Allocation-release confirm: shows what will happen before the
  // PATCH/DELETE leaves the browser so the operator can back out.
  // intent === 'shrink' carries a target qty; 'delete' just removes.
  const [releaseConfirm, setReleaseConfirm] = useState(null);
  // Canonical source_system tag list for the source_system dropdown.
  // Fetched once; ADMIN + so-full-edit-override roles can repoint.
  const [sourceSystems, setSourceSystems] = useState([]);

  useEffect(() => { loadOrders(); }, [page, statusFilter, search]);  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    api.get('/admin/source-systems', { silentPermissionDenied: true }).then(async (res) => {
      if (!res?.ok) return;
      const data = await res.json();
      setSourceSystems(data.source_systems || []);
    });
  }, []);

  // Debounced SKU typeahead matching the PO edit modal. Quiet when the
  // edit modal is closed; minimum 2 characters to avoid spamming the
  // items endpoint on every keystroke.
  useEffect(() => {
    if (!editing) return;
    const sku = newLineSku.trim();
    if (sku.length < 2) {
      setSkuSuggestions([]);
      setResolvedItem(null);
      return;
    }
    const handle = setTimeout(async () => {
      const res = await api.get(
        `/admin/items?q=${encodeURIComponent(sku)}&per_page=10&active=true`,
        { silentPermissionDenied: true },
      );
      if (!res?.ok) {
        setSkuSuggestions([]);
        setResolvedItem(null);
        return;
      }
      const data = await res.json();
      const items = data.items || [];
      setSkuSuggestions(items);
      const exact = items.find(
        (i) => String(i.sku || '').trim().toLowerCase() === sku.toLowerCase(),
      ) || null;
      setResolvedItem(exact);
    }, 200);
    return () => clearTimeout(handle);
  }, [newLineSku, editing]);

  async function loadOrders() {
    const qp = new URLSearchParams({ page: String(page), per_page: '50' });
    if (statusFilter !== 'All') qp.set('status', statusFilter);
    if (search) qp.set('q', search);
    const res = await api.get(`/admin/sales-orders?${qp}`);
    if (res?.ok) {
      const data = await res.json();
      setOrders(data.sales_orders || []);
      setPagination({ page: data.page, pages: data.pages, total: data.total });
    }
  }

  async function viewSO(so) {
    const res = await api.get(`/admin/sales-orders/${so.so_id}`);
    if (res?.ok) {
      const data = await res.json();
      setSelectedSO(data.sales_order);
      setSOLines(data.lines || []);
    }
  }

  async function openEdit(so) {
    // Fetch the full SO with its lines so the modal can drive line
    // CRUD without a second round-trip. Falls back to the row-level
    // values if the detail call fails so the operator at least sees
    // the basic edit form.
    const res = await api.get(`/admin/sales-orders/${so.so_id}`);
    let full = so;
    let lines = [];
    let pickTasks = [];
    if (res?.ok) {
      const data = await res.json();
      full = data.sales_order || so;
      lines = data.lines || [];
      // so-refinement: pick_tasks in PICKED state. Populates the
      // status-revert modal when the operator demotes status.
      pickTasks = data.pick_tasks || [];
    }
    // Attach pick_tasks to the editing object so the revert modal can
    // read them without a second fetch.
    setEditing({ ...full, _pick_tasks: pickTasks });
    // so-refinement: address fields share editForm so the edit modal
    // can save header + addresses in one click. PATCH /address keeps
    // its own backend status gate; saveEdit fires it conditionally.
    const addressInit = {};
    for (const key of ADDRESS_FIELD_KEYS) addressInit[key] = full[key] || '';
    setEditForm({
      so_number: full.so_number || '',
      customer_name: full.customer_name || '',
      customer_phone: full.customer_phone || '',
      ship_address: full.ship_address || '',
      ship_method: full.ship_method || '',
      ship_by_date: full.ship_by_date ? full.ship_by_date.slice(0, 10) : '',
      memo: full.memo || '',
      status: full.status || 'OPEN',
      source_system: full.source_system || '',
      // Server-computed company-local Shipped Date (YYYY-MM-DD). Seeds
      // the date picker directly so it matches the read-only view.
      shipped_at: full.shipped_date_local || '',
      // mig 063: free-text upstream-origin label. Blank when the
      // connector mapping has not been wired up yet.
      order_origin: full.order_origin || '',
      // so-refinement: tracking_number defaults blank and prefills with
      // whatever dockd ship wrote (or whatever ADMIN backfilled).
      tracking_number: full.tracking_number || '',
      ...addressInit,
    });
    setEditLines(lines);
    setLineErrors({});
    setNewLineSku('');
    setNewLineQty('');
    setNewLineError('');
    setReleaseConfirm(null);
    setEditError('');
  }

  function closeEdit() {
    setEditing(null);
    setEditLines([]);
    setLineErrors({});
    setNewLineSku('');
    setNewLineQty('');
    setNewLineError('');
    setReleaseConfirm(null);
    setEditError('');
    setConfirmCancel(false);
    setRevertConfirm(null);
  }

  // Build the PUT body for header edits, optionally skipping the
  // status field (used when the status change has already gone through
  // the revert-status endpoint and only header sidecar fields remain).
  function _buildHeaderBody({ includeStatus }) {
    const body = {
      so_number: editForm.so_number,
      customer_name: editForm.customer_name || null,
      customer_phone: editForm.customer_phone || null,
      ship_address: editForm.ship_address || null,
      ship_method: editForm.ship_method || null,
      ship_by_date: editForm.ship_by_date || null,
      memo: editForm.memo || null,
    };
    if (includeStatus && editForm.status && editForm.status !== editing.status) {
      body.status = editForm.status;
    }
    const trackingTrimmed = (editForm.tracking_number || '').trim();
    if (trackingTrimmed !== (editing.tracking_number || '')) {
      body.tracking_number = trackingTrimmed || null;
    }
    if (hasSOFullEdit && editForm.source_system !== (editing.source_system || '')) {
      body.source_system = editForm.source_system || '';
    }
    // Shipped Date: send only when the operator actually changed it,
    // comparing against the server-computed company-local date. The
    // backend anchors the picked date at noon in the company timezone,
    // so a precise ship instant is never clobbered by a routine save.
    // Empty clears to NULL.
    const shippedDate = (editForm.shipped_at || '').trim();
    if (shippedDate !== (editing.shipped_date_local || '')) {
      body.shipped_at = shippedDate || null;
    }
    // mig 063: same trim-and-diff treatment as source_system. Empty
    // string clears the column to NULL.
    const originTrimmed = (editForm.order_origin || '').trim();
    if (originTrimmed !== (editing.order_origin || '')) {
      body.order_origin = originTrimmed || null;
    }
    return body;
  }

  function _buildAddressBody() {
    const addressBody = {};
    let addressChanged = false;
    for (const key of ADDRESS_FIELD_KEYS) {
      const next = editForm[key] || '';
      const prev = editing[key] || '';
      if (next !== prev) addressChanged = true;
      addressBody[key] = next;
    }
    return { addressBody, addressChanged };
  }

  // Fires the header PUT and (if any field changed) the address PATCH.
  // Returns true on full success; on failure, sets editError and
  // returns false. Reused by both the no-revert path and the
  // post-revert path.
  async function _commitHeaderAndAddress({ includeStatus }) {
    const body = _buildHeaderBody({ includeStatus });
    const { addressBody, addressChanged } = _buildAddressBody();
    const putRes = await api.put(`/admin/sales-orders/${editing.so_id}`, body);
    if (!putRes?.ok) {
      let data = null;
      try { data = await putRes?.json(); } catch (_) { /* non-JSON body */ }
      setEditError(formatApiError(data, 'Failed to save'));
      return false;
    }
    if (addressChanged) {
      const patchRes = await api.patch(
        `/admin/sales-orders/${editing.so_id}/address`,
        addressBody,
      );
      if (!patchRes?.ok) {
        let data = null;
        try { data = await patchRes?.json(); } catch (_) { /* non-JSON body */ }
        setEditError(formatApiError(data, 'Header saved, but failed to save addresses'));
        loadOrders();
        return false;
      }
    }
    return true;
  }

  async function saveEdit() {
    setEditError('');
    // so-refinement: detect a backward status transition from
    // PICKED/PACKED/SHIPPED. The revert-status endpoint owns the pick /
    // pack / ship unwind, so we intercept here and let the operator
    // pick which pick_tasks release back to their source bin before the
    // status flip + sidecar header saves go through.
    const cur = editing.status;
    const next = editForm.status;
    if (
      cur && next && cur !== next
      && REVERTABLE_STATUSES.has(cur)
      && STATUS_ORDER[next] !== undefined
      && STATUS_ORDER[cur] !== undefined
      && STATUS_ORDER[next] < STATUS_ORDER[cur]
    ) {
      const pickTasks = editing._pick_tasks || [];
      setRevertConfirm({
        newStatus: next,
        currentStatus: cur,
        pickTasks,
        // Default: keep all (every row checked). Operator unchecks
        // the specific picks they want to release back to source bin.
        // If target < PICKED, the backend rejects while any keep
        // remains, so the operator sees the inconsistency before commit.
        keepIds: new Set(pickTasks.map((t) => t.pick_task_id)),
        error: '',
        busy: false,
      });
      return;
    }
    const ok = await _commitHeaderAndAddress({ includeStatus: true });
    if (!ok) return;
    closeEdit();
    loadOrders();
  }

  // so-refinement: open the revert modal in release-only mode. No
  // status change implied; the modal posts new_status == current and
  // the backend skips the status update + audit row when nothing
  // changed. Default keep-all matches the revert-mode default;
  // operator unchecks the specific rows they want to release back
  // to bin.
  function openReleaseOnly(pickTasks) {
    if (!editing || !pickTasks.length) return;
    setRevertConfirm({
      newStatus: editing.status,
      currentStatus: editing.status,
      pickTasks,
      keepIds: new Set(pickTasks.map((t) => t.pick_task_id)),
      error: '',
      busy: false,
      mode: 'release-only',
    });
  }

  async function confirmRevertAndSave() {
    if (!revertConfirm) return;
    setRevertConfirm({ ...revertConfirm, error: '', busy: true });
    // Anything NOT in keepIds is released back to its source bin.
    const releaseIds = revertConfirm.pickTasks
      .filter((t) => !revertConfirm.keepIds.has(t.pick_task_id))
      .map((t) => t.pick_task_id);
    const revertRes = await api.post(
      `/admin/sales-orders/${editing.so_id}/revert-status`,
      {
        new_status: revertConfirm.newStatus,
        release_pick_task_ids: releaseIds,
      },
    );
    if (!revertRes?.ok) {
      let data = null;
      try { data = await revertRes?.json(); } catch (_) { /* non-JSON body */ }
      setRevertConfirm({
        ...revertConfirm,
        busy: false,
        error: data?.error || 'Failed to revert status',
      });
      return;
    }
    // Release-only mode: no status change, no in-flight header edits to
    // chain. Refresh the SO detail in place so quantity_picked +
    // pick_tasks reflect the release, then close the revert modal and
    // leave the main edit modal open for any further work.
    if (revertConfirm.mode === 'release-only') {
      const refresh = await api.get(`/admin/sales-orders/${editing.so_id}`);
      if (refresh?.ok) {
        const data = await refresh.json();
        setEditing({ ...(data.sales_order || editing), _pick_tasks: data.pick_tasks || [] });
        setEditLines(data.lines || []);
      }
      setRevertConfirm(null);
      loadOrders();
      return;
    }
    // Status change committed via revert; commit any remaining header /
    // address edits (without re-sending status) and tear down.
    const ok = await _commitHeaderAndAddress({ includeStatus: false });
    if (!ok) {
      // Header save failed after revert succeeded. Status already
      // changed, so close the revert modal and let the operator see
      // the editError on the main modal.
      setRevertConfirm(null);
      loadOrders();
      return;
    }
    closeEdit();
    loadOrders();
  }

  // ── line CRUD ─────────────────────────────────────────────────────────────

  async function resolveSku(sku) {
    const key = String(sku || '').trim().toLowerCase();
    if (!key) return null;
    const res = await api.get(
      `/admin/items?q=${encodeURIComponent(sku)}&per_page=10&active=true`,
      { silentPermissionDenied: true },
    );
    if (!res?.ok) return null;
    const data = await res.json();
    return (data.items || []).find(
      (i) => String(i.sku || '').trim().toLowerCase() === key,
    ) || null;
  }

  async function addLine() {
    setNewLineError('');
    const sku = newLineSku.trim();
    const qty = parseInt(newLineQty, 10);
    if (!sku) { setNewLineError('Enter a SKU'); return; }
    if (isNaN(qty) || qty <= 0) { setNewLineError('Enter a positive quantity'); return; }
    setAddingLine(true);
    try {
      let item = resolvedItem;
      if (!item || String(item.sku).toLowerCase() !== sku.toLowerCase()) {
        item = await resolveSku(sku);
      }
      if (!item) { setNewLineError(`Unknown SKU: ${sku}`); return; }
      const res = await api.post(
        `/admin/sales-orders/${editing.so_id}/lines`,
        { item_id: item.item_id, quantity_ordered: qty },
      );
      if (res?.ok) {
        const created = await res.json();
        setEditLines((ls) => [
          ...ls,
          { ...created, item_name: item.item_name, upc: item.upc },
        ]);
        setNewLineSku('');
        setNewLineQty('');
        setResolvedItem(null);
        setSkuSuggestions([]);
      } else {
        let data = null;
        try { data = await res?.json(); } catch (_) { /* non-JSON body */ }
        setNewLineError(formatApiError(data, 'Failed to add line'));
      }
    } finally {
      setAddingLine(false);
    }
  }

  // Commits a quantity_ordered edit. If new qty crosses below
  // quantity_allocated, we route through the release-confirm modal so
  // the operator explicitly acknowledges that pick-batch allocations
  // will be unwound.
  function requestLineQtyEdit(line, newQty) {
    if (newQty < (line.quantity_allocated || 0) && (line.quantity_allocated || 0) > 0) {
      setReleaseConfirm({ intent: 'shrink', line, newQty });
      return;
    }
    return commitLineQtyEdit(line, newQty);
  }

  async function commitLineQtyEdit(line, newQty) {
    setLineErrors((e) => ({ ...e, [line.so_line_id]: '' }));
    const res = await api.patch(
      `/admin/sales-orders/${editing.so_id}/lines/${line.so_line_id}`,
      { quantity_ordered: newQty },
    );
    if (res?.ok) {
      const data = await res.json();
      setEditLines((ls) => ls.map((l) =>
        l.so_line_id === line.so_line_id
          ? {
              ...l,
              quantity_ordered: newQty,
              // The server zeroes quantity_allocated when it releases.
              quantity_allocated: data.released_allocation > 0 ? 0 : l.quantity_allocated,
            }
          : l,
      ));
    } else {
      let data = null;
      try { data = await res?.json(); } catch (_) { /* non-JSON body */ }
      setLineErrors((e) => ({
        ...e,
        [line.so_line_id]: formatApiError(data, 'Failed to update'),
      }));
    }
  }

  function requestRemoveLine(line) {
    if ((line.quantity_allocated || 0) > 0) {
      setReleaseConfirm({ intent: 'delete', line });
      return;
    }
    return commitRemoveLine(line);
  }

  async function commitRemoveLine(line) {
    setLineErrors((e) => ({ ...e, [line.so_line_id]: '' }));
    const res = await api.delete(
      `/admin/sales-orders/${editing.so_id}/lines/${line.so_line_id}`,
    );
    if (res?.ok) {
      setEditLines((ls) => ls.filter((l) => l.so_line_id !== line.so_line_id));
    } else {
      let data = null;
      try { data = await res?.json(); } catch (_) { /* non-JSON body */ }
      setLineErrors((e) => ({
        ...e,
        [line.so_line_id]: formatApiError(data, 'Failed to remove'),
      }));
    }
  }

  async function confirmReleaseAndProceed() {
    const c = releaseConfirm;
    setReleaseConfirm(null);
    if (!c) return;
    if (c.intent === 'shrink') await commitLineQtyEdit(c.line, c.newQty);
    else if (c.intent === 'delete') await commitRemoveLine(c.line);
  }

  // Admin virtual pick. Seeds one empty entry per unpicked line so
  // the operator only needs to fill the rows they want to pick now,
  // then fetches available bins per line in parallel. The endpoint
  // returns at most one entry per (line, bin) but the operator can
  // add a second entry on the same line to split a pick across two
  // bins -- bins remain reusable in the dropdown because the backend
  // sums per-line + per-bin at submit time.
  async function openAdminPick() {
    const form = {};
    const lineIds = [];
    for (const line of editLines || []) {
      const unpicked = (line.quantity_ordered || 0) - (line.quantity_picked || 0);
      if (unpicked > 0) {
        form[line.so_line_id] = [{ bin_id: '', quantity: '' }];
        lineIds.push(line.so_line_id);
      }
    }
    setAdminPickForm(form);
    setAdminPickBinsByLine({});
    setAdminPickError('');
    setAdminPicking(editing);

    const fetched = {};
    await Promise.all(lineIds.map(async (solId) => {
      const res = await api.get(
        `/admin/sales-orders/${editing.so_id}/lines/${solId}/available-bins`,
        { silentPermissionDenied: true },
      );
      if (res?.ok) {
        const data = await res.json();
        fetched[solId] = data.bins || [];
      } else {
        fetched[solId] = [];
      }
    }));
    setAdminPickBinsByLine(fetched);
  }

  function closeAdminPick() {
    setAdminPicking(null);
    setAdminPickForm({});
    setAdminPickBinsByLine({});
    setAdminPickError('');
    setAdminPickSubmitting(false);
  }

  function addAdminPickBin(solId) {
    setAdminPickForm((prev) => ({
      ...prev,
      [solId]: [...(prev[solId] || []), { bin_id: '', quantity: '' }],
    }));
  }

  function removeAdminPickBin(solId, idx) {
    setAdminPickForm((prev) => {
      const next = [...(prev[solId] || [])];
      next.splice(idx, 1);
      // Keep at least one row visible so the operator can re-add a
      // bin without re-opening the modal; an empty row is harmless
      // because the submit pass drops blanks before posting.
      return { ...prev, [solId]: next.length ? next : [{ bin_id: '', quantity: '' }] };
    });
  }

  function updateAdminPickField(solId, idx, field, value) {
    setAdminPickForm((prev) => {
      const next = [...(prev[solId] || [])];
      next[idx] = { ...next[idx], [field]: value };
      return { ...prev, [solId]: next };
    });
  }

  async function submitAdminPick() {
    setAdminPickError('');
    const lines = [];
    for (const [solId, entries] of Object.entries(adminPickForm)) {
      for (const e of entries) {
        const qty = parseInt(e.quantity, 10);
        const binId = parseInt(e.bin_id, 10);
        if (!e.bin_id || !binId || isNaN(qty) || qty <= 0) continue;
        lines.push({
          so_line_id: Number(solId),
          bin_id: binId,
          quantity: qty,
        });
      }
    }
    if (lines.length === 0) {
      setAdminPickError('Pick at least one line: choose a bin and a quantity.');
      return;
    }
    setAdminPickSubmitting(true);
    try {
      const res = await api.post(
        `/admin/sales-orders/${adminPicking.so_id}/admin-pick`,
        { lines },
      );
      if (!res?.ok) {
        let data = null;
        try { data = await res?.json(); } catch (_) { /* non-JSON */ }
        setAdminPickError(formatApiError(data, 'Admin pick failed'));
        return;
      }
      closeAdminPick();
      closeEdit();
      loadOrders();
    } finally {
      setAdminPickSubmitting(false);
    }
  }

  async function cancelSO() {
    setEditError('');
    const res = await api.post(`/admin/sales-orders/${editing.so_id}/cancel`, {});
    if (res?.ok) {
      setConfirmCancel(false);
      setEditing(null);
      loadOrders();
    } else {
      const data = await res?.json();
      setEditError(data?.error || 'Failed to cancel order');
      setConfirmCancel(false);
    }
  }

  const columns = [
    { key: 'so_number', label: 'SO Number', mono: true },
    { key: 'customer_name', label: 'Customer' },
    { key: 'ship_by_date', label: 'Ship By', mono: true, render: (r) => r.ship_by_date ? formatDateOnly(r.ship_by_date) : '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'created_at', label: 'Created', render: (r) => r.created_at ? new Date(r.created_at).toLocaleDateString() : '-' },
    { key: 'actions', label: '', render: (r) => (
      <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); openEdit(r); }} aria-label="Edit" title="Edit">&#9998;</button>
    )},
  ];

  const thStyle = { textAlign: 'left', padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 };
  const tdStyle = { padding: '6px 8px' };

  return (
    <div>
      <PageHeader title="Sales Orders" />

      <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
        <label style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Status:</label>
        <select className="form-select" value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }} style={{ width: 160 }}>
          {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <input
          className="form-input"
          style={{ maxWidth: 320 }}
          placeholder="Search by SO number or customer"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
        />
      </div>

      <DataTable
        columns={columns}
        data={orders}
        pagination={pagination}
        onPageChange={setPage}
        onRowClick={viewSO}
        emptyMessage="No sales orders found"
      />

      {selectedSO && (
        <Modal
          title={`SO ${selectedSO.so_number}`}
          onClose={() => { setSelectedSO(null); setSOLines([]); }}
          footer={<button className="btn" onClick={() => { setSelectedSO(null); setSOLines([]); }}>Close</button>}
          size="wide"
        >
          <section className="section">
            <div className="section-title">Order Summary</div>
            <div className="detail-grid" style={{ marginBottom: 0 }}>
              <span className="detail-label">Customer</span><span>{selectedSO.customer_name || '-'}</span>
              <span className="detail-label">Status</span><span><StatusTag status={selectedSO.status} /></span>
              {/* mig 063: free-text upstream-origin label populated
                  by the inbound payload mapping. */}
              <span className="detail-label">Source:</span>
              <span><NullableValue value={selectedSO.order_origin} /></span>
              <span className="detail-label">Ship By</span><span className="mono">{selectedSO.ship_by_date ? formatDateOnly(selectedSO.ship_by_date) : '-'}</span>
              <span className="detail-label">Ship Method</span><span>{selectedSO.ship_method || '-'}</span>
              {/* v1.8.0 (#282) per-order cost fields. order_total +
                  customer_shipping_paid arrive as strings on the wire to
                  preserve Decimal precision; render literal. */}
              <span className="detail-label">Order Total</span>
              <span className="mono"><NullableValue value={selectedSO.order_total != null ? `$${selectedSO.order_total}` : null} /></span>
              {/* so-refinement: tracking # under the cost fields. */}
              <span className="detail-label">Tracking #</span>
              <span className="mono"><NullableValue value={selectedSO.tracking_number} /></span>
              <span className="detail-label">Shipping Paid</span>
              <span className="mono"><NullableValue value={selectedSO.customer_shipping_paid != null ? `$${selectedSO.customer_shipping_paid}` : null} /></span>
              {/* so-refinement: legacy ship_address row dropped from
                  the Order Summary -- the structured Shipping Address
                  card below is the operator-facing source of truth.
                  The column itself stays populated for the mobile
                  pick/pack/ship floor screens that still read it. */}
            </div>
          </section>

          {/* v1.9.0 #315: free-text operator-facing note. Only shown
              when populated; render with whiteSpace: pre-wrap so
              embedded newlines from the source ERP survive. */}
          {selectedSO.memo && (
            <section className="section">
              <div style={{
                padding: 10,
                borderLeft: '3px solid #b87333', backgroundColor: '#fdf6ed',
                whiteSpace: 'pre-wrap',
              }}>
                <div style={{
                  fontSize: 11, fontWeight: 700, color: '#b87333',
                  letterSpacing: 0.4, marginBottom: 4,
                }}>NOTE</div>
                <div style={{ fontSize: 13, lineHeight: 1.4 }}>{selectedSO.memo}</div>
              </div>
            </section>
          )}

          {/* v1.8.0 (#268) per-component billing + shipping addresses.
              Each side gets its own card so a half-populated address
              renders cleanly without column shifts. so-refinement:
              address edits live in the main Edit modal now. */}
          <section className="section">
            <div className="section-title">Addresses</div>
            <div style={{
              display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16,
            }}>
              <div className="card">
                <div className="card-title">Billing Address</div>
                <div className="detail-grid" style={{ marginBottom: 0 }}>
                  {ADDRESS_FIELD_KEYS.filter((k) => k.startsWith('billing_')).map((k) => (
                    <span key={k} style={{ display: 'contents' }}>
                      <span className="detail-label">{ADDRESS_FIELD_LABELS[k]}</span>
                      <span><NullableValue value={selectedSO[k]} /></span>
                    </span>
                  ))}
                </div>
              </div>
              <div className="card">
                <div className="card-title">Shipping Address</div>
                <div className="detail-grid" style={{ marginBottom: 0 }}>
                  {ADDRESS_FIELD_KEYS.filter((k) => k.startsWith('shipping_')).map((k) => (
                    <span key={k} style={{ display: 'contents' }}>
                      <span className="detail-label">{ADDRESS_FIELD_LABELS[k]}</span>
                      <span><NullableValue value={selectedSO[k]} /></span>
                    </span>
                  ))}
                </div>
              </div>
            </div>
          </section>

          <section className="section" style={{ marginBottom: 0 }}>
            <div className="section-title">Line Items</div>
            {soLines.length > 0 ? (
              <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--border)' }}>
                    <th style={thStyle}>SKU</th>
                    <th style={thStyle}>Item Name</th>
                    <th style={{ ...thStyle, textAlign: 'right' }}>Ordered</th>
                    <th style={{ ...thStyle, textAlign: 'right' }}>Picked</th>
                    <th style={{ ...thStyle, textAlign: 'right' }}>Shipped</th>
                  </tr>
                </thead>
                <tbody>
                  {soLines.map((l, i) => (
                    <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td className="mono" style={tdStyle}>{l.sku}</td>
                      <td style={tdStyle}>{l.item_name}</td>
                      <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.quantity_ordered}</td>
                      <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.quantity_picked}</td>
                      <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.quantity_shipped}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No line items</p>
            )}
          </section>
        </Modal>
      )}

      {editing && (() => {
        const status = editing.status;
        const headerEditable = isAdmin || hasSOFullEdit || status === 'OPEN';
        const lineEditable = headerEditable && !LINE_TERMINAL_STATUSES.has(status);
        const sourceSystemEditable = hasSOFullEdit;
        // so-refinement: PATCH /address gate is ADMIN any status,
        // non-admin only OPEN. Backend still enforces; this just keeps
        // the disabled state in sync so users don't try a save that
        // will 403.
        const addressEditable = isAdmin || status === 'OPEN';
        return (
          <Modal
            title={`Edit SO ${editing.so_number}`}
            onClose={closeEdit}
            size="wide"
            footer={
              <>
                {status === 'OPEN' && (
                  <button className="btn btn-danger" onClick={() => setConfirmCancel(true)}>Cancel Order</button>
                )}
                {/* Admin virtual pick: operator marks the SO picked
                    without the handheld. Gated to OPEN + ADMIN-or-
                    so-full-edit because the backend rejects every
                    other shape. Mirrors the backend gate exactly so
                    the button does not show when it would 403. */}
                {status === 'OPEN' && (isAdmin || hasSOFullEdit) && (
                  <button
                    className="btn btn-warning"
                    onClick={openAdminPick}
                    title="Mark this order picked via admin (no handheld)"
                  >
                    Admin Pick
                  </button>
                )}
                {/* so-refinement: shortcut to the release modal that
                    does not require flipping status first. Visible only
                    when the SO actually has picks in flight. */}
                {(editing._pick_tasks || []).length > 0 && (
                  <button
                    className="btn"
                    onClick={() => openReleaseOnly(editing._pick_tasks || [])}
                    title="Release picked inventory back to source bins without changing status"
                  >Release Picked Quantities</button>
                )}
                <button className="btn" onClick={closeEdit}>Cancel</button>
                <button className="btn btn-primary" onClick={saveEdit} disabled={!headerEditable}>Save</button>
              </>
            }
          >
            {editError && <div className="form-error" style={{ marginBottom: 12 }}>{editError}</div>}
            {!headerEditable && (
              <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
                Header edits are locked while the SO is {status}. Ask an
                admin to grant the so-full-edit override if a post-OPEN
                correction is needed.
              </p>
            )}
            {headerEditable && status !== 'OPEN' && (
              <p style={{ fontSize: 12, color: 'var(--copper)', marginBottom: 12 }}>
                Editing past OPEN ({status}). Line changes that shrink
                or remove allocated quantity will release pick-batch
                allocations and require a re-pick.
              </p>
            )}
            <div className="form-row">
              <div className="form-group">
                <label>SO Number</label>
                <input className="form-input" disabled={!headerEditable} value={editForm.so_number} onChange={(e) => setEditForm({ ...editForm, so_number: e.target.value })} />
              </div>
              <div className="form-group">
                <label>Customer</label>
                <input className="form-input" disabled={!headerEditable} value={editForm.customer_name} onChange={(e) => setEditForm({ ...editForm, customer_name: e.target.value })} />
              </div>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label>Phone</label>
                <input className="form-input" disabled={!headerEditable} value={editForm.customer_phone} onChange={(e) => setEditForm({ ...editForm, customer_phone: e.target.value })} />
              </div>
              <div className="form-group">
                <label>Ship By</label>
                <input className="form-input" type="date" disabled={!headerEditable} value={editForm.ship_by_date} onChange={(e) => setEditForm({ ...editForm, ship_by_date: e.target.value })} />
              </div>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label>Status</label>
                <select
                  className="form-select"
                  disabled={!headerEditable}
                  value={editForm.status}
                  onChange={(e) => setEditForm({ ...editForm, status: e.target.value })}
                >
                  {EDITABLE_STATUS_OPTIONS.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>
              <div className="form-group">
                <label>Source System</label>
                <select
                  className="form-select"
                  disabled={!sourceSystemEditable}
                  value={editForm.source_system || ''}
                  onChange={(e) => setEditForm({ ...editForm, source_system: e.target.value })}
                  title={sourceSystemEditable
                    ? 'Reassign the ERP source tag (audit-logged)'
                    : 'ADMIN or so-full-edit override required to reassign'}
                >
                  <option value="">(none)</option>
                  {sourceSystems.map((s) => (
                    <option key={s.source_system} value={s.source_system}>
                      {s.source_system} {s.kind ? `(${s.kind})` : ''}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            {/* mig 063: free-text upstream-origin label. No allowlist /
                dropdown -- the inbound payload populates it; ADMIN /
                so-full-edit can override. Empty string clears the column. */}
            <div className="form-group">
              <label>Source:</label>
              <input
                className="form-input"
                disabled={!headerEditable}
                maxLength={64}
                placeholder="e.g. amazon, shopify-store-1, phone-order"
                value={editForm.order_origin}
                onChange={(e) => setEditForm({ ...editForm, order_origin: e.target.value })}
              />
            </div>
            <div className="form-group">
              <label>Ship Method</label>
              <input className="form-input" disabled={!headerEditable} value={editForm.ship_method} onChange={(e) => setEditForm({ ...editForm, ship_method: e.target.value })} />
            </div>
            {/* so-refinement: Tracking # on its own row, right-aligned
                beneath Ship Method to match the view-modal layout. */}
            <div className="form-row">
              <div className="form-group" aria-hidden="true" />
              <div className="form-group">
                <label>Tracking #</label>
                <input
                  className="form-input mono"
                  disabled={!headerEditable}
                  maxLength={128}
                  placeholder="Auto-fills from Dockd on ship"
                  value={editForm.tracking_number}
                  onChange={(e) => setEditForm({ ...editForm, tracking_number: e.target.value })}
                />
              </div>
            </div>
            <div className="form-group">
              <label>Ship Address</label>
              <textarea className="form-input" rows={2} disabled={!headerEditable} value={editForm.ship_address} onChange={(e) => setEditForm({ ...editForm, ship_address: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Note (memo)</label>
              <textarea
                className="form-input" rows={3}
                placeholder="......"
                maxLength={4096}
                disabled={!headerEditable}
                value={editForm.memo}
                onChange={(e) => setEditForm({ ...editForm, memo: e.target.value })}
              />
            </div>

            {/* so-refinement: addresses live inside the edit modal now.
                Saved via PATCH /address from saveEdit when any of the 16
                fields changed. Empty string clears to NULL. */}
            <section className="section" style={{ marginTop: 16 }}>
              <div className="section-title">Addresses</div>
              {!addressEditable && (
                <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
                  Address edits are locked while the SO is {status}. Only
                  ADMIN can edit addresses past OPEN.
                </p>
              )}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
                <div>
                  <strong style={{ display: 'block', marginBottom: 8 }}>Billing</strong>
                  {ADDRESS_FIELD_KEYS.filter((k) => k.startsWith('billing_')).map((k) => (
                    <div key={k} className="form-group">
                      <label>{ADDRESS_FIELD_LABELS[k]}</label>
                      <input
                        className="form-input"
                        disabled={!addressEditable}
                        value={editForm[k] || ''}
                        onChange={(e) => setEditForm({ ...editForm, [k]: e.target.value })}
                      />
                    </div>
                  ))}
                </div>
                <div>
                  <strong style={{ display: 'block', marginBottom: 8 }}>Shipping</strong>
                  {ADDRESS_FIELD_KEYS.filter((k) => k.startsWith('shipping_')).map((k) => (
                    <div key={k} className="form-group">
                      <label>{ADDRESS_FIELD_LABELS[k]}</label>
                      <input
                        className="form-input"
                        disabled={!addressEditable}
                        value={editForm[k] || ''}
                        onChange={(e) => setEditForm({ ...editForm, [k]: e.target.value })}
                      />
                    </div>
                  ))}
                </div>
              </div>
            </section>

            <section className="section" style={{ marginTop: 16 }}>
              <div className="section-title">Line Items</div>
              {editLines.length > 0 ? (
                <table className="lines-table">
                  <thead>
                    <tr>
                      <th>SKU</th>
                      <th>Item Name</th>
                      <th style={{ textAlign: 'right' }}>Ordered</th>
                      <th style={{ textAlign: 'right' }}>Allocated</th>
                      <th style={{ textAlign: 'right' }}>Picked</th>
                      <th style={{ textAlign: 'right' }}>Shipped</th>
                      <th style={{ width: 40 }}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {editLines.map((l) => {
                      const removable = lineEditable
                        && (l.quantity_picked || 0) === 0
                        && (l.quantity_packed || 0) === 0
                        && (l.quantity_shipped || 0) === 0;
                      return (
                        <tr key={l.so_line_id}>
                          <td className="mono">{l.sku}</td>
                          <td>{l.item_name}</td>
                          <td style={{ textAlign: 'right' }}>
                            <LineQtyInput
                              line={l}
                              disabled={!lineEditable}
                              onCommit={requestLineQtyEdit}
                            />
                            {lineErrors[l.so_line_id] && (
                              <div className="form-error" style={{ fontSize: 11, marginTop: 4, textAlign: 'right' }}>
                                {lineErrors[l.so_line_id]}
                              </div>
                            )}
                          </td>
                          <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_allocated || 0}</td>
                          <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_picked || 0}</td>
                          <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_shipped || 0}</td>
                          <td style={{ textAlign: 'right' }}>
                            <button
                              className="btn btn-sm btn-danger"
                              onClick={() => requestRemoveLine(l)}
                              disabled={!removable}
                              title={!removable
                                ? (LINE_TERMINAL_STATUSES.has(status)
                                    ? `Lines locked while SO is ${status}`
                                    : 'Line has picked/packed/shipped units; unwind first')
                                : 'Remove line'}
                              aria-label="Remove line"
                            >&#10005;</button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No line items yet.</p>
              )}

              {lineEditable && (
                <div style={{
                  marginTop: 12, padding: 12,
                  background: 'var(--surface)',
                  borderRadius: 'var(--radius)',
                }}>
                  <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                    <div style={{ flex: '0 0 240px' }}>
                      <label style={{ fontSize: 11, color: 'var(--text-secondary)', fontWeight: 500 }}>SKU</label>
                      <input
                        className="form-input mono"
                        placeholder="Type SKU to search"
                        list="so-edit-sku-suggestions"
                        value={newLineSku}
                        onChange={(e) => setNewLineSku(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') addLine(); }}
                      />
                      <datalist id="so-edit-sku-suggestions">
                        {skuSuggestions.map((it) => (
                          <option key={it.item_id} value={it.sku}>{it.item_name}</option>
                        ))}
                      </datalist>
                    </div>
                    <div style={{ flex: '0 0 120px' }}>
                      <label style={{ fontSize: 11, color: 'var(--text-secondary)', fontWeight: 500 }}>Quantity</label>
                      <input
                        className="form-input"
                        type="number"
                        min={1}
                        placeholder="0"
                        value={newLineQty}
                        onChange={(e) => setNewLineQty(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') addLine(); }}
                      />
                    </div>
                    <div style={{ flexGrow: 1 }} />
                    <button
                      className="btn btn-primary"
                      onClick={addLine}
                      disabled={addingLine}
                      style={{ marginTop: 16 }}
                    >
                      {addingLine ? 'Adding...' : 'Add Line'}
                    </button>
                  </div>
                  {newLineError && (
                    <div className="form-error" style={{ marginTop: 8, fontSize: 12 }}>
                      {newLineError}
                    </div>
                  )}
                  {!newLineError && resolvedItem && (
                    <div style={{ marginTop: 8, fontSize: 12, color: 'var(--success)' }}>
                      Found: <strong>{resolvedItem.sku}</strong> - {resolvedItem.item_name}
                    </div>
                  )}
                  {!newLineError && !resolvedItem && newLineSku.trim().length >= 2 && skuSuggestions.length === 0 && (
                    <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-secondary)' }}>
                      No matches for "{newLineSku.trim()}". Click Add Line to retry the lookup.
                    </div>
                  )}
                </div>
              )}
            </section>
          </Modal>
        );
      })()}

      {releaseConfirm && (
        <Modal
          title="Release pick-batch allocation?"
          onClose={() => setReleaseConfirm(null)}
          footer={
            <>
              <button className="btn" onClick={() => setReleaseConfirm(null)}>Back</button>
              <button className="btn btn-danger" onClick={confirmReleaseAndProceed}>
                {releaseConfirm.intent === 'delete' ? 'Release + Remove Line' : 'Release + Update'}
              </button>
            </>
          }
        >
          <p style={{ fontSize: 13, marginBottom: 8 }}>
            Line <strong>{releaseConfirm.line.sku}</strong> currently has{' '}
            <strong>{releaseConfirm.line.quantity_allocated}</strong> units allocated to a pick batch.
          </p>
          <p style={{ fontSize: 13, marginBottom: 8 }}>
            {releaseConfirm.intent === 'delete'
              ? 'Removing this line will release those units back to the source bins. The line will then be deleted.'
              : `Reducing quantity to ${releaseConfirm.newQty} (below the allocated ${releaseConfirm.line.quantity_allocated}) will release the full line allocation back to the source bins.`}
          </p>
          <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
            Released inventory becomes available for re-allocation on the next pick-batch create. The action is audit-logged.
          </p>
        </Modal>
      )}

      {confirmCancel && editing && (
        <Modal
          title={`Cancel order ${editing.so_number}?`}
          onClose={() => setConfirmCancel(false)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmCancel(false)}>Keep Order</button>
              <button className="btn btn-danger" onClick={cancelSO}>Cancel Order</button>
            </>
          }
        >
          <p style={{ fontSize: 13 }}>
            Cancel this order? It will no longer appear in picking/shipping queues.
            This action cannot be undone from the UI.
          </p>
        </Modal>
      )}

      {/* Admin virtual-pick sub-modal. Per-line bin dropdown + qty
          input. The "+ bin" button adds a second {bin, qty} row to the
          same line so the operator can split a pick across two bins in
          one submit. Available-bins dropdown is sorted preferred-first
          by the backend. Submit is all-or-nothing. */}
      {adminPicking && (
        <Modal
          title={`Admin pick ${adminPicking.so_number}`}
          onClose={closeAdminPick}
          size="wide"
          footer={
            <>
              <button className="btn" onClick={closeAdminPick} disabled={adminPickSubmitting}>
                Cancel
              </button>
              <button
                className="btn btn-warning"
                onClick={submitAdminPick}
                disabled={adminPickSubmitting}
              >
                {adminPickSubmitting ? 'Picking...' : 'Pick'}
              </button>
            </>
          }
        >
          {adminPickError && (
            <div className="form-error" style={{ marginBottom: 12 }}>{adminPickError}</div>
          )}
          <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
            Pick this order virtually -- the backend writes the same line
            counters, inventory decrement, and audit row as a real pick.
            Adds a synthetic pick task per entry so the existing Release
            Picked Quantities modal can undo it later.
          </p>
          {Object.keys(adminPickForm).length === 0 ? (
            <p style={{ fontSize: 13 }}>Every line is already fully picked.</p>
          ) : (
            <table className="data-table" style={{ marginBottom: 12 }}>
              <thead>
                <tr>
                  <th>Line</th>
                  <th>SKU</th>
                  <th>Item</th>
                  <th style={{ textAlign: 'right' }}>Ordered</th>
                  <th style={{ textAlign: 'right' }}>Picked</th>
                  <th>Bin</th>
                  <th style={{ width: 100 }}>Qty</th>
                  <th style={{ width: 100 }}></th>
                </tr>
              </thead>
              <tbody>
                {(editLines || []).flatMap((line) => {
                  const entries = adminPickForm[line.so_line_id];
                  if (!entries) return [];
                  const bins = adminPickBinsByLine[line.so_line_id] || [];
                  const unpicked = (line.quantity_ordered || 0) - (line.quantity_picked || 0);
                  return entries.map((entry, idx) => (
                    <tr key={`${line.so_line_id}-${idx}`}>
                      {idx === 0 ? (
                        <>
                          <td rowSpan={entries.length}>{line.line_number}</td>
                          <td rowSpan={entries.length} className="mono">{line.sku}</td>
                          <td rowSpan={entries.length}>{line.item_name}</td>
                          <td rowSpan={entries.length} style={{ textAlign: 'right' }}>{line.quantity_ordered}</td>
                          <td rowSpan={entries.length} style={{ textAlign: 'right' }}>{line.quantity_picked}</td>
                        </>
                      ) : null}
                      <td>
                        <select
                          className="form-select"
                          value={entry.bin_id}
                          onChange={(e) => updateAdminPickField(
                            line.so_line_id, idx, 'bin_id', e.target.value,
                          )}
                        >
                          <option value="">- choose a bin -</option>
                          {bins.map((b) => (
                            <option key={b.bin_id} value={b.bin_id}>
                              {b.bin_code}
                              {b.zone_name ? ` (${b.zone_name})` : ''}
                              {' '}- avail {b.quantity_available}
                              {b.preferred_priority != null ? ' [pref]' : ''}
                            </option>
                          ))}
                        </select>
                        {bins.length === 0 && (
                          <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 4 }}>
                            No bins with available stock in this warehouse.
                          </div>
                        )}
                      </td>
                      <td>
                        <input
                          type="number"
                          className="form-input"
                          min={1}
                          max={unpicked}
                          step={1}
                          value={entry.quantity}
                          onChange={(e) => updateAdminPickField(
                            line.so_line_id, idx, 'quantity', e.target.value,
                          )}
                          placeholder="0"
                        />
                      </td>
                      <td>
                        <div style={{ display: 'flex', gap: 4 }}>
                          <button
                            type="button"
                            className="btn btn-sm"
                            onClick={() => addAdminPickBin(line.so_line_id)}
                            title="Pick remainder from a second bin"
                          >+ bin</button>
                          {entries.length > 1 && (
                            <button
                              type="button"
                              className="btn btn-sm btn-danger"
                              onClick={() => removeAdminPickBin(line.so_line_id, idx)}
                              title="Drop this bin entry"
                              aria-label="Remove entry"
                            >&#10005;</button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ));
                })}
              </tbody>
            </table>
          )}
        </Modal>
      )}

      {revertConfirm && editing && (() => {
        const { newStatus, currentStatus, pickTasks, keepIds, error, busy, mode } = revertConfirm;
        const releaseOnly = mode === 'release-only';
        // No unship / unpack when target == current (release-only).
        const willUnship = !releaseOnly && currentStatus === 'SHIPPED';
        const willUnpack = !releaseOnly && (
          STATUS_ORDER[currentStatus] >= STATUS_ORDER.PACKED
          && STATUS_ORDER[newStatus] < STATUS_ORDER.PACKED
        );
        const targetBelowPicked = STATUS_ORDER[newStatus] < STATUS_ORDER.PICKED;
        // heldCount = picks the operator is keeping (checked). Those
        // block a target below PICKED since the SO would still have
        // quantity_picked on its lines.
        const heldCount = keepIds.size;
        const releaseCount = pickTasks.length - heldCount;
        const blockedByHeld = targetBelowPicked && heldCount > 0;
        const totalReleaseUnits = pickTasks
          .filter((t) => !keepIds.has(t.pick_task_id))
          .reduce((acc, t) => acc + (t.quantity_picked || 0), 0);
        function toggleId(id) {
          const next = new Set(keepIds);
          if (next.has(id)) next.delete(id); else next.add(id);
          setRevertConfirm({ ...revertConfirm, keepIds: next });
        }
        return (
          <Modal
            title={releaseOnly
              ? `Release picked quantities - SO ${editing.so_number}`
              : `Revert SO ${editing.so_number}: ${currentStatus} -> ${newStatus}`}
            onClose={() => setRevertConfirm(null)}
            size="wide"
            footer={
              <>
                <button className="btn" onClick={() => setRevertConfirm(null)} disabled={busy}>Back</button>
                <button
                  className="btn btn-primary"
                  onClick={confirmRevertAndSave}
                  disabled={busy || blockedByHeld}
                  title={blockedByHeld
                    ? `Cannot demote to ${newStatus} with ${heldCount} pick(s) checked as Keep. Uncheck them or pick a target at PICKED or higher.`
                    : 'Release unchecked picks and save'}
                >
                  {busy ? 'Reverting...' : 'Release & Save'}
                </button>
              </>
            }
          >
            {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}

            {(willUnship || willUnpack) && (
              <div style={{
                padding: 10, marginBottom: 12,
                borderLeft: '3px solid var(--copper)', backgroundColor: '#fdf6ed',
              }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--copper)', letterSpacing: 0.4, marginBottom: 4 }}>
                  SIDE EFFECTS
                </div>
                <ul style={{ fontSize: 13, lineHeight: 1.5, paddingLeft: 18, margin: 0 }}>
                  {willUnship && (
                    <li>
                      <strong>Unship:</strong> tracking number, carrier, and shipped-at
                      will clear on the header. Physical inventory was already
                      shipped; reconcile externally if the package is returning.
                    </li>
                  )}
                  {willUnpack && (
                    <li>
                      <strong>Unpack:</strong> packed quantity zeroes on every line.
                      No inventory moves (pack does not touch bin stock).
                    </li>
                  )}
                </ul>
              </div>
            )}

            <p style={{ fontSize: 13, marginBottom: 8 }}>
              {pickTasks.length === 0
                ? <>No PICKED units to release.{releaseOnly ? '' : ` The revert will just change the status${willUnpack ? ' and unpack' : ''}${willUnship ? ' and unship' : ''}.`}</>
                : <>This SO has <strong>{pickTasks.length}</strong> picked task(s) totalling <strong>{pickTasks.reduce((acc, t) => acc + (t.quantity_picked || 0), 0)}</strong> units across the bins below. All are kept PICKED by default; <strong>uncheck</strong> the Keep box for any task you want to release back to bin.</>
              }
            </p>

            {pickTasks.length > 0 && (
              <table className="lines-table" style={{ marginTop: 8 }}>
                <thead>
                  <tr>
                    <th style={{ width: 56, textAlign: 'center' }}>Keep</th>
                    <th>SKU</th>
                    <th>Item</th>
                    <th>Bin</th>
                    <th style={{ textAlign: 'right' }}>Qty</th>
                    <th>Picked At</th>
                  </tr>
                </thead>
                <tbody>
                  {pickTasks.map((t) => (
                    <tr key={t.pick_task_id}>
                      <td style={{ textAlign: 'center' }}>
                        <input
                          type="checkbox"
                          checked={keepIds.has(t.pick_task_id)}
                          onChange={() => toggleId(t.pick_task_id)}
                          disabled={busy}
                          title="Check to keep this pick (unchecked releases back to bin)"
                        />
                      </td>
                      <td className="mono">{t.sku}</td>
                      <td>{t.item_name}</td>
                      <td className="mono">{t.bin_code}</td>
                      <td className="mono" style={{ textAlign: 'right' }}>{t.quantity_picked}</td>
                      <td className="mono" style={{ fontSize: 12 }}>
                        {t.picked_at ? new Date(t.picked_at).toLocaleString() : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {blockedByHeld && (
              <p style={{ fontSize: 12, color: 'var(--danger)', marginTop: 12 }}>
                Target status <strong>{newStatus}</strong> requires zero picked
                units. {heldCount} task(s) are checked to keep - uncheck them
                or change the target to PICKED or higher.
              </p>
            )}
            {!blockedByHeld && pickTasks.length > 0 && (
              <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 12 }}>
                Releasing <strong>{releaseCount}</strong> of {pickTasks.length} task(s),
                returning <strong>{totalReleaseUnits}</strong> units to their bins.
                The action is audit-logged per task.
              </p>
            )}
          </Modal>
        );
      })()}
    </div>
  );
}

// Matches PurchaseOrders.LineQtyInput - inline qty input with on-blur
// commit so an Enter or click-away triggers the PATCH. Re-syncs to
// the parent's value after a successful update so a server-side rewrite
// (e.g. allocation release zeroing the line) reflects immediately.
function LineQtyInput({ line, disabled, onCommit }) {
  const [val, setVal] = useState(String(line.quantity_ordered));
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setVal(String(line.quantity_ordered));
  }, [line.quantity_ordered]);

  async function commit() {
    const n = parseInt(val, 10);
    if (isNaN(n) || n <= 0 || n === line.quantity_ordered) {
      setVal(String(line.quantity_ordered));
      return;
    }
    setSaving(true);
    await onCommit(line, n);
    setSaving(false);
  }

  return (
    <input
      type="number"
      min={1}
      className="form-input mono"
      style={{ width: 88, textAlign: 'right', padding: '4px 8px' }}
      value={val}
      disabled={disabled || saving}
      onChange={(e) => setVal(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur(); }}
    />
  );
}
