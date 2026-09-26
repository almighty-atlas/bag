# ADR-0019: The worker schedules purge and garbage collection itself

**Status:** accepted
**Date:** 2026-09-26

## Context

Spec section 5.8 calls purge "a background job". `bag purge` and `bag gc` exist
as explicit commands (ADR-0013), but a self-hosted Compose stack has no scheduler
unless the operator adds cron, and forgotten maintenance means the trash never
empties and orphaned objects accumulate.

## Decision

The worker process runs a second thread that, every `BAG_MAINTENANCE_INTERVAL_HOURS`
(default 24, `0` disables it), executes purge with the configured retentions and
then storage garbage collection with the default one-hour minimum age. The first
run happens one interval after start, failures are logged and never stop the job
loop, and worker readiness reports both threads. The CLI commands remain for
manual or cron-driven runs and for dry runs.

## Consequences

Default deployments stay clean without extra infrastructure. Running several
workers means several schedules, which is safe because purge re-checks under
row locks and GC holds the capture locks; it only wastes a little work. Operators
who prefer cron set the interval to zero. There is still no visibility of the
last run beyond the log line; a status endpoint can follow if needed.
