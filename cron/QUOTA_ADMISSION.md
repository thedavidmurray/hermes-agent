# Shared provider quota admission

Native cron agent jobs and Kanban workers can share a provider quota hold. This is
opt-in and does not change interactive chat admission, script-only (`no_agent`)
cron jobs, provider credential pools, or existing task failure accounting.

Configure the participating profile through its native `config.yaml` settings:

```yaml
quota_admission:
  enabled: true
  provider_domains:
    anthropic: hive-subscription
  cooldown_seconds: 900
  probe_lease_seconds: 1800
```

Only the explicitly mapped provider is covered. Cron uses the job's provider
override, then the profile's configured model provider. Kanban uses the task's
provider override, then the **assigned worker profile's** configured provider.
An automatic/unset provider is not inferred from credentials. `default_domain`
can explicitly cover all of the profile's automated work, including unresolved
provider selections. Labels describe an operator-chosen quota boundary; Hermes
does not inspect credentials or infer account identity to choose a domain.

The default state file is `<profile home>/quota-admission.json`. Sharing across
profiles requires each participating profile to explicitly configure the same
absolute `state_file` and domain label. Different domain labels in that file
remain independently available. Use a local filesystem supporting native file
locks and atomic replacement. Unreadable/invalid state or lock failure defers
configured work rather than discarding the hold.

A structured `rate_limit` or `upstream_rate_limit` agent failure closes its
domain. Startup Codex quota `AuthError` failures also close it. Provider reset
timestamps and retry-after hints determine the hold; `cooldown_seconds` is used
when that evidence has no reset time. A failed recovery probe holds for at least
`cooldown_seconds` even when the provider hint is shorter: pooled gateways report
their shortest per-minute window while the pool stays exhausted. Generated response text, broad worker exit
codes, timeouts, and billing errors alone never establish a shared quota hold.
This state is scheduling evidence, not actual spend: subscription usage is not
converted into a dollar amount.

While held, another cron occurrence is parked without delivery, advancing its
failure count, or consuming a finite repeat. Kanban tasks remain ready/review.
`no_agent` watchdog jobs continue to run. Once the deadline passes, an atomic
reservation admits one recovery probe in that domain. Typed successful completion
reopens it; a failed or inconclusive probe preserves/extends its hold. Other
attempts wait. A crashed/abandoned probe reservation expires after
`probe_lease_seconds`, even if the reserving gateway process is still alive.
Generation and token fencing prevent an older result from resetting newer state.
Expiry does not kill already-running work: choose the lease long enough for a
normal recovery attempt. Work admitted before the first confirmed failure is
also allowed to finish.

Rollout requires the updated native cron and worker code in the running Hermes
installation and the explicit profile configuration above. A running process
must reload the code through the normal restart procedure. There is no extra
daemon, environment feature switch, account probe, or scheduler to activate.
