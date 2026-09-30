# Implementation plan: community garden plot booking

## Configuration and wave overview

- Initial spec / plan reviews: 1 / 1
- Waves: wave 1: T5; wave 2: T1, T2; wave 3: T3, T4 (deferred docs excluded)

## Task DAG

### Task T1: booking data model

- Wave: 1
- Dependencies: none
- Owned files: `app/models/plot.py`; `app/models/booking.py` (new)
- Acceptance criteria: covers R1 and R2; unit tests for overlapping bookings

### Task T2: booking overlap rule

- Wave: 1
- Dependencies: T1
- Owned files: `app/services/booking.py` (new)
- Acceptance criteria: covers R2; refuses a second active booking

### Task T3: waitlist order

- Wave: 2
- Dependencies: T1
- Owned files: `app/services/waitlist.py`
- Acceptance criteria: covers R3; arrival order for a full plot

### Task T4: overlap checks

- Wave: 2
- Dependencies: T1; T2
- Owned files: `app/services/overlap.py` (new)
- Acceptance criteria: covers R1; unit tests for overlapping bookings

Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Gardeners reserve a plot for the season and keep shared tools in the shed.
Also covers R2.
### Task T5: season calendar view

- Wave: 3
- Dependencies: T3, T4
- Owned files: `app/views/calendar.py` (new)
- Acceptance criteria: covers R2; shows free plots for the season
