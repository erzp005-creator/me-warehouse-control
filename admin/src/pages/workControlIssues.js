const ISSUE_TYPES = new Set([
  'WRONG_ITEM',
  'WRONG_QUANTITY',
  'WRONG_ORDER',
  'DAMAGED_ITEM',
  'LABEL_ERROR',
  'SKU_NOT_FOUND',
  'OTHER',
]);

export function prepareWorkIssue(task, warehouseId, form) {
  const errorType = String(form.error_type || '').trim().toUpperCase();
  const description = String(form.description || '').trim();
  const orderReference = String(form.order_reference || '').trim();
  const sku = String(form.sku || '').trim();
  const rawQuantity = String(form.quantity ?? '').trim();

  if (!task?.task_id) return { error: 'The current task is missing. Refresh and try again.' };
  if (!Number.isInteger(Number(warehouseId)) || Number(warehouseId) < 1) return { error: 'Choose a warehouse first.' };
  if (!ISSUE_TYPES.has(errorType)) return { error: 'Choose an issue type.' };
  if (!description) return { error: 'Describe what happened so the reviewer can verify it.' };

  let quantity = null;
  if (rawQuantity !== '') {
    quantity = Number(rawQuantity);
    if (!Number.isInteger(quantity) || quantity < 1) return { error: 'Quantity must be a whole number of at least 1.' };
  }

  return {
    payload: {
      warehouse_id: Number(warehouseId),
      task_id: Number(task.task_id),
      batch_id: task.batch_id ? Number(task.batch_id) : undefined,
      error_type: errorType,
      severity: 'MEDIUM',
      discovered_stage: task.task_type || undefined,
      courier_barcode: orderReference || undefined,
      sku: sku || undefined,
      quantity: quantity || undefined,
      description,
    },
  };
}

