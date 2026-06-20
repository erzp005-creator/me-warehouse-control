-- ============================================================
-- Least-privilege DB role for connector-publisher (Pipe C)
-- ============================================================
-- Pipe C adds a connector-publisher daemon that (1) consumes
-- integration_events to recompute per-channel sellable availability
-- and (2) debounce-publishes the dirty rows to each channel's HTTP
-- sink. It follows the V-214 #153 (snapshot-keeper) and #169
-- (webhook-dispatcher) shape so a compromise of the api host does not
-- grant the publisher's permissions, and vice versa.
--
-- Grants cover exactly the publisher's need-to-know:
--
--   * CONNECT on the database (and USAGE on the public schema)
--   * SELECT on integration_events (cursor read: "what changed?")
--   * SELECT on inventory, items, bins, warehouses (recompute the
--     sellable number from current state)
--   * SELECT / UPDATE on channels (read config; auto-pause status
--     flip with pause_reason on ceiling breach)
--   * SELECT / INSERT / UPDATE on channel_availability (materialize,
--     version bump, publish-state transitions, DLQ park)
--   * SELECT / UPDATE on channel_recompute_state (advance the cursor)
--   * USAGE on channel_availability_version_seq (nextval stamps
--     current_version; without USAGE the UPDATE surfaces a
--     'permission denied for sequence' error)
--   * LISTEN on integration_events_visible (migration 031) -- not
--     granted explicitly; any role with CONNECT can LISTEN. Listed
--     here for operator awareness.
--
-- Explicitly NOT granted:
--
--   * users, wms_tokens, wms_tokens_audit, audit_log, connectors
--     (writes) -- the publisher has no admin / token / forensic
--     write surface; a compromise must not reach the auth or audit
--     layers. Channel config-change audit rows are written by the
--     admin endpoints under the api role, not by this daemon.
--   * webhook_subscriptions / webhook_deliveries / webhook_secrets --
--     Pipe A is the dispatcher's surface, not the publisher's.
--   * any INSERT/UPDATE/DELETE on inventory -- the publisher is a
--     read-only observer of stock; it never mutates it.
--
-- This script is operator-driven, not auto-applied. Migrations cannot
-- read a password from the environment and we do not bake a
-- placeholder password into git. Run it once per deployment after the
-- v1.30.0 upgrade; the operator supplies the password via a psql
-- variable.
--
--   psql -v sentry_publisher_password=<strong-password> \
--        -U <db-superuser> -d <database> \
--        -f db/role-publisher.sql
--
-- (Pass the password unquoted; the script wraps it as an SQL string
-- literal via psql's :'var' interpolation at the top level, NOT inside
-- a DO $$ ... $$ block, because psql does not substitute :'var' inside
-- dollar-quoted strings. The two CREATE / ALTER ROLE branches are
-- guarded by WHERE clauses so exactly one fires per run.)
--
-- Then set PUBLISHER_DATABASE_URL in .env to
--   postgresql://sentry_publisher:<strong-password>@db:5432/<database>
-- and restart the connector-publisher container.
--
-- Idempotent: safe to re-run. Both \gexec branches are predicated on
-- pg_roles existence so re-runs only ALTER (which carries the new
-- password); the GRANTs are themselves idempotent.
--
-- ON_ERROR_STOP discipline: a failure exits non-zero so deployment
-- automation flags the step (the V-214 #170 regression shipped
-- because psql exited 0 despite role-creation failures).
-- ============================================================

\set ON_ERROR_STOP on

-- Branch 1: CREATE ROLE on first run.
SELECT format('CREATE ROLE sentry_publisher LOGIN PASSWORD %L',
              :'sentry_publisher_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentry_publisher')
\gexec

-- Branch 2: ALTER ROLE on subsequent runs (also rotates the password
-- if the operator supplies a new value).
SELECT format('ALTER ROLE sentry_publisher WITH LOGIN PASSWORD %L',
              :'sentry_publisher_password')
 WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentry_publisher')
\gexec

-- GRANT CONNECT needs a literal database name; derive it from
-- current_database() so the script works regardless of POSTGRES_DB.
SELECT format('GRANT CONNECT ON DATABASE %I TO sentry_publisher',
              current_database())
\gexec

GRANT USAGE ON SCHEMA public TO sentry_publisher;

GRANT SELECT                 ON integration_events       TO sentry_publisher;
GRANT SELECT                 ON inventory                TO sentry_publisher;
GRANT SELECT                 ON items                    TO sentry_publisher;
GRANT SELECT                 ON bins                     TO sentry_publisher;
GRANT SELECT                 ON warehouses               TO sentry_publisher;
GRANT SELECT, UPDATE         ON channels                 TO sentry_publisher;
GRANT SELECT, INSERT, UPDATE ON channel_availability     TO sentry_publisher;
GRANT SELECT, UPDATE         ON channel_recompute_state  TO sentry_publisher;
GRANT USAGE                  ON SEQUENCE channel_availability_version_seq TO sentry_publisher;
