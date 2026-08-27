export function prepareReceivingEntry(selected, form, photo) {
  const expected = form.expected === '' ? null : Number(form.expected);
  const received = form.received === '' ? null : Number(form.received);
  const damaged = Number(form.damaged || 0);
  if (!selected) return { error: 'Choose a SKU from the SiteGiant catalog.' };
  if (!Number.isInteger(received) || received < 0) return { error: 'Enter a received quantity of 0 or more.' };
  if (expected !== null && (!Number.isInteger(expected) || expected < 0)) return { error: 'Expected quantity must be 0 or more.' };
  if (!Number.isInteger(damaged) || damaged < 0 || damaged > received) return { error: 'Damaged quantity must be between 0 and the received quantity.' };
  if (!photo) return { error: `Take or upload one arrival photo for ${selected.sku}.` };
  return {
    entry: {
      sku: selected.sku,
      item_name: selected.item_name,
      expected_quantity: expected,
      received_quantity: received,
      good_quantity: received - damaged,
      damaged_quantity: damaged,
      notes: form.note.trim() || null,
      photo,
      catalog: selected,
    },
  };
}

