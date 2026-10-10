# schedule_windows — local-time windows (v0.39 Phase 1)

Pure classification, no I/O, no clock (the instant is an argument). Zone:
`APP_TIMEZONE` (default `Europe/Prague`, needs the `tzdata` package on Windows).

Precedence **QUIET > WORK > DISCRETIONARY**:
- QUIET: `QUIET_HOURS_START → QUIET_HOURS_END` daily (crosses midnight).
- WORK: Mon–Fri that is not a public holiday of `HOLIDAY_COUNTRY` (`holidays`
  library, `CZ`), from quiet end until `WORKDAY_END`. The stretch before
  `WORKDAY_START` also counts as WORK (not an evening, so never discretionary).
- DISCRETIONARY: workday evenings (`WORKDAY_END → quiet start`) and the whole
  non-quiet day on weekends and public holidays.

Fail-closed: if the holiday calendar cannot load, weekdays stay working days. DST is
handled through zoneinfo (tests cover 2026-03-29 and 2026-10-25). Layout is validated
at construction: `QUIET_END ≤ WORKDAY_START < WORKDAY_END < QUIET_START`.
