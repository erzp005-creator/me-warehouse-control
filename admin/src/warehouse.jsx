import { createContext, useContext, useState, useEffect } from 'react';
import { api } from './api.js';
import { useAuth } from './auth.jsx';

const WarehouseContext = createContext(null);

export function WarehouseProvider({ children }) {
  const { user } = useAuth();
  const [warehouses, setWarehouses] = useState([]);
  const [warehouseId, setWarehouseIdState] = useState(() => {
    const saved = sessionStorage.getItem('sentry_warehouse_id');
    return saved ? Number(saved) : null;
  });

  useEffect(() => {
    if (!user) return;
    let cancelled = false;

    // USER accounts commonly do not have permission to list every warehouse.
    // Their login payload already contains the warehouse scope, so initialise
    // the active warehouse from that scope before the optional catalogue call.
    // This also prevents a stale admin sessionStorage selection from leaving a
    // floor employee on a warehouse they cannot access.
    const allowedWarehouseIds = [user.warehouse_id, ...(user.warehouse_ids || [])]
      .map(Number)
      .filter((id, index, ids) => Number.isInteger(id) && id > 0 && ids.indexOf(id) === index);
    const saved = sessionStorage.getItem('sentry_warehouse_id');
    const savedId = saved ? Number(saved) : null;

    async function loadWarehouses() {
      if (user.role !== 'ADMIN' && allowedWarehouseIds.length > 0) {
        const scopedId = allowedWarehouseIds.includes(savedId) ? savedId : allowedWarehouseIds[0];
        setWarehouseIdState(scopedId);
        sessionStorage.setItem('sentry_warehouse_id', String(scopedId));
        setWarehouses(allowedWarehouseIds.map((id) => ({
          warehouse_id: id,
          warehouse_code: `WH-${String(id).padStart(2, '0')}`,
          warehouse_name: `Warehouse ${id}`,
        })));
      }

      // P6.1: topbar warehouse picker needs this list but a USER without
      // the "warehouses" page grant should not see the global modal.
      // The scoped placeholder above remains available if this call is denied.
      const res = await api.get('/admin/warehouses', { silentPermissionDenied: true });
      if (!res?.ok || cancelled) return;
      const data = await res.json();
      const fullList = data.warehouses || [];
      const list = user.role === 'ADMIN' || allowedWarehouseIds.length === 0
        ? fullList
        : fullList.filter((warehouse) => allowedWarehouseIds.includes(
          Number(warehouse.warehouse_id || warehouse.id),
        ));
      setWarehouses(list);
      // Auto-select first warehouse if none selected or saved one no longer exists
      if (list.length > 0) {
        const currentSaved = sessionStorage.getItem('sentry_warehouse_id');
        const currentSavedId = currentSaved ? Number(currentSaved) : null;
        const exists = list.some((w) => (w.warehouse_id || w.id) === currentSavedId);
        if (!exists) {
          const firstId = list[0].warehouse_id || list[0].id;
          setWarehouseIdState(firstId);
          sessionStorage.setItem('sentry_warehouse_id', String(firstId));
        }
      }
    }

    loadWarehouses();
    return () => { cancelled = true; };
  }, [user]);

  function setWarehouseId(id) {
    setWarehouseIdState(id);
    sessionStorage.setItem('sentry_warehouse_id', String(id));
  }

  const warehouse = warehouses.find((w) => (w.warehouse_id || w.id) === warehouseId) || null;

  return (
    <WarehouseContext.Provider value={{ warehouses, warehouseId, warehouse, setWarehouseId }}>
      {children}
    </WarehouseContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useWarehouse() {
  return useContext(WarehouseContext);
}

