# ME Warehouse SiteGiant Hourly Bridge

This Chrome extension reads the four package-stage totals shown on the signed-in
SiteGiant dashboard once per hour and posts them to ME Warehouse Control.

It is deliberately read-only:

- no SiteGiant password is stored;
- no SiteGiant order or inventory endpoint is called;
- the Warehouse Control token can only call `sitegiant.capture` for its assigned
  warehouse;
- repeated captures in the same hour are idempotent.

## Install in the Austin Chrome profile

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Select **Load unpacked** and choose this `sitegiant-bridge` folder.
4. In Warehouse Control, open **Admin → Tokens** and create a token with Warehouse
   1 and only the `sitegiant.capture` endpoint.
5. Open the extension settings, paste the one-time token, save, then select
   **Capture now**.

Chrome must be running and the Austin profile must remain signed into SiteGiant.
The Work Control page marks the feed stale after 90 minutes without a successful
reading.
