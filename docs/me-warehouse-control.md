# ME Warehouse Control

ME Warehouse Control is a work-allocation and evidence layer built on the
Apache-2.0 licensed Sentry WMS v1.37.0 codebase. It is deliberately separate
from SiteGiant's order, stock and accounting records.

The extension answers four operational questions:

1. Which employee owns the current work?
2. How much active time did that work take after legitimate pauses are removed?
3. Which picker and packer touched a Pack Note when a mistake is discovered?
4. What did an employee physically count on arrival, and what photo evidence
   was sent to the stock clerk?

No KPI score, ranking or commission formula is applied in this release. The
report exposes factual workload, active time, excluded pause time and confirmed
mistakes only.

## Pack Note workflow

- A supervisor creates one batch for one Pack Note, with 1–50 order numbers and
  their courier barcodes.
- The system creates independent `PICKING` and `PACKING` tasks immediately. Two
  different employees may therefore work on the same Pack Note concurrently.
- An employee receives one task at a time according to priority, explicit
  assignment and their granted work types.
- Picking and packing cannot start until the employee scans any one order or
  courier barcode contained in that Pack Note. This is enforced by the API,
  not only by the mobile interface.
- Completing 100% of a task atomically completes its timing record and claims
  the next suitable task.

The supervisor page accepts pasted CSV, tab-separated or pipe-separated rows:

```text
order_number,courier_barcode,sku_count,unit_count
TTS-10001,MY123456789,3,5
TTS-10002,MY123456790,1,1
```

The Pack Note reference can continue to use the current Google Sheet row
number, for example `2950`.

## Time rules

Task states are `QUEUED`, `ASSIGNED`, `CLAIMED`, `IN_PROGRESS`, `PAUSED`,
`COMPLETED` and `CANCELLED`. Every state change is append-only in
`work_task_events` and also written to the tamper-resistant audit log.

Only `IN_PROGRESS` time contributes to active seconds. A pause requires a
reason in the mobile UI, such as waiting for stock, system/printer delay,
supervisor request or break. Pause time remains visible but is excluded from
active work time.

## Mistake and cross-check workflow

- A worker reports an exception against the task, Pack Note, order and/or SKU.
- The system stores the picker and packer who claimed the two tasks for that
  Pack Note.
- A photo can be attached from the handheld camera.
- A supervisor confirms or dismisses the case and selects responsibility:
  picker, packer, both, supplier, source data, system or unknown.
- Only confirmed reviews appear in the employee factual efficiency report.

This keeps a reported issue from becoming an automatic accusation.

## Receiving / Draft GRN workflow

1. The employee starts a `RECEIVING` task.
2. They enter or scan each SKU and record expected, received, good and damaged
   quantities. Short and over quantities are calculated automatically.
3. The app creates a Draft GRN, uploads at least one arrival photo, and then
   submits it to the stock clerk.
4. The linked task completes and the employee receives the next suitable task
   immediately. Inventory is not posted by this action.
5. The stock clerk approves/rejects the draft and later marks it `POSTED` after
   recording it in the source WMS. Rejection automatically assigns a higher
   priority recount task to the original counter.

## Permissions

In **Admin → Users**, grant:

- `Work Queue` to show the employee's work screen.
- One or more operational functions: `Pick`, `Pack`, `Receive`, `Put-Away`, or
  `Count`. These are enforced by the API when claiming work.
- `Work Control` web-page permission to supervisors or stock clerks who should
  create batches, view the live queue, and review receiving or mistake cases.

Administrators have all permissions.

## Installation

### Fresh local installation

Copy `.env.example` to `.env`, set every required secret, then run:

```bash
docker compose up -d --build
```

The fresh PostgreSQL bootstrap includes migration 082 automatically. Evidence
photos are stored in the persistent `work_evidence` Docker volume.

### Existing database

Back up PostgreSQL first, then run:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/082_work_control.sql
docker compose down
docker compose up -d --build
```

For a standalone API deployment, set `EVIDENCE_STORAGE_DIR` to an absolute,
persistent directory writable by the API process. Back up this directory with
the database. Database rows store SHA-256, MIME type and byte size; the actual
photo bytes are stored on disk.

### Mobile app

```bash
cd mobile
npm install
npx expo start
```

The upstream EAS project ownership has been removed. Before the first cloud
APK build, sign in to the company's Expo account and run `eas init` once.

## Main API surface

All endpoints are under `/api/work-control`:

- `POST/GET /batches`
- `GET /scan/<barcode>`
- `POST /tasks/claim-next`
- `POST /tasks/<id>/verify-scan`
- `POST /tasks/<id>/transition`
- `POST/GET /errors` and `POST /errors/<id>/review`
- `POST/GET /receiving-drafts`
- `POST /receiving-drafts/<id>/submit`
- `POST /receiving-drafts/<id>/review`
- `POST/GET /evidence`
- `GET /reports/efficiency`

The batch-create endpoint is the integration seam for a future SiteGiant or
Google Sheet adapter. It accepts a stable `source_system`, `pack_note_ref`,
order number, courier barcode and workload counts, and is idempotent per
warehouse/source/Pack Note.

## Current integration boundary

This release supports supervisor paste/import and REST creation of Pack Note
batches. It does not yet pull from SiteGiant automatically because the exact
SiteGiant API credentials, event format and Pack Note field mapping have not
been supplied. The work-control tables never update SiteGiant inventory or
order status, so adding that adapter later will not change the employee
workflow.
