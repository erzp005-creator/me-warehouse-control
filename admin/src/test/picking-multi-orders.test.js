import { describe, it, expect } from 'vitest';
import { shippingGroupKey, groupOrdersByAddress } from '../pages/pickingGroups.js';

// Multi-Orders groups orders that ship to the same address so they can be
// boxed together. The key must fold trivial formatting variants of one
// address together while keeping genuinely different destinations apart --
// and it must produce the identical result on the list page and the print
// page, which is why both import this one helper.

const base = {
  shipping_address_line1: '12 Elm St',
  shipping_address_line2: '',
  shipping_address_city: 'Denver',
  shipping_address_state: 'CO',
  shipping_address_postal_code: '80202',
};

describe('shippingGroupKey', () => {
  it('folds case / whitespace / trailing-punctuation variants of one address', () => {
    const a = shippingGroupKey(base);
    const b = shippingGroupKey({
      ...base,
      shipping_address_line1: '12 elm st.',
      shipping_address_city: '  denver ',
      shipping_address_state: 'co',
    });
    expect(a).toBe(b);
  });

  it('matches ZIP+4 against the same 5-digit door', () => {
    const a = shippingGroupKey(base);
    const b = shippingGroupKey({ ...base, shipping_address_postal_code: '80202-1234' });
    expect(a).toBe(b);
  });

  it('keeps different zips in different groups', () => {
    const a = shippingGroupKey(base);
    const b = shippingGroupKey({ ...base, shipping_address_postal_code: '80203' });
    expect(a).not.toBe(b);
  });

  it('keeps a different street line in a different group', () => {
    const a = shippingGroupKey(base);
    const b = shippingGroupKey({ ...base, shipping_address_line1: '14 Elm St' });
    expect(a).not.toBe(b);
  });

  it('is ungroupable (null) when structured line1 is present but postal is missing', () => {
    expect(shippingGroupKey({ ...base, shipping_address_postal_code: '' })).toBeNull();
  });

  it('falls back to the legacy ship_address string when structured line1 is absent', () => {
    const a = shippingGroupKey({ ship_address: '99 Oak Ave\nBoulder CO 80301' });
    const b = shippingGroupKey({ ship_address: '99 oak ave\nboulder co 80301' });
    expect(a).toBe(b);
    expect(a).toContain('legacy:');
  });

  it('is ungroupable (null) for an order with no address at all', () => {
    expect(shippingGroupKey({})).toBeNull();
    expect(shippingGroupKey(null)).toBeNull();
  });
});

describe('groupOrdersByAddress', () => {
  const at = (over) => ({ ...base, ...over });

  it('returns only groups of two or more; singletons are dropped', () => {
    const orders = [
      at({ so_id: 1, so_number: '1001', ship_by_date: '2026-07-15' }),
      at({ so_id: 2, so_number: '1002', ship_by_date: '2026-07-15' }),
      at({ so_id: 3, so_number: '1003', shipping_address_postal_code: '80203' }), // lone, other zip
    ];
    const groups = groupOrdersByAddress(orders);
    expect(groups).toHaveLength(1);
    expect(groups[0].orders.map((o) => o.so_id).sort()).toEqual([1, 2]);
  });

  it('excludes ungroupable (null-key) orders even if several share the gap', () => {
    const orders = [
      at({ so_id: 1, shipping_address_line1: '', shipping_address_postal_code: '' }),
      at({ so_id: 2, shipping_address_line1: '', shipping_address_postal_code: '' }),
    ];
    expect(groupOrdersByAddress(orders)).toHaveLength(0);
  });

  it('orders groups by earliest ship_by_date (most urgent combined shipment first)', () => {
    const orders = [
      // Group A: earliest 07-20
      at({ so_id: 1, ship_by_date: '2026-07-20' }),
      at({ so_id: 2, ship_by_date: '2026-07-22' }),
      // Group B (different door): earliest 07-16 -> should sort first
      at({ so_id: 3, shipping_address_line1: '5 Pine Rd', ship_by_date: '2026-07-16' }),
      at({ so_id: 4, shipping_address_line1: '5 Pine Rd', ship_by_date: '2026-07-18' }),
    ];
    const groups = groupOrdersByAddress(orders);
    expect(groups).toHaveLength(2);
    expect(groups[0].orders[0].shipping_address_line1).toBe('5 Pine Rd');
  });

  it('preserves incoming member order within a group', () => {
    const orders = [
      at({ so_id: 30, ship_by_date: '2026-07-15' }),
      at({ so_id: 10, ship_by_date: '2026-07-15' }),
      at({ so_id: 20, ship_by_date: '2026-07-15' }),
    ];
    const groups = groupOrdersByAddress(orders);
    expect(groups[0].orders.map((o) => o.so_id)).toEqual([30, 10, 20]);
  });
});
