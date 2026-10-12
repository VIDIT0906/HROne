# REVIEW.md

Defects found in the starter `app/main.py`.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Default client returns naive datetimes; "local" and UTC get mixed everywhere. | Punch times shift by 5h30 depending on server time zone; naive vs aware `TypeError` when subtracting. | `tz_aware=True`; all instants UTC-aware, converted to IST only for R1-R3. |
| 2 | `compute_late_minutes` | Floors to minutes *then* tests `> 10`, so 09:40:01 gives 10 and is treated as on time (R2 says strictly more than 10:00). | Punch-in at 09:40:01 on a 09:30 shift returns 0. | Compare in seconds (`> 600`), then floor. |
| 3 | `compute_late_minutes` | Shift start is anchored to the punch's own calendar day, not the attendance date; ignores IST and overnight shifts. | 22:00 shift punched at 02:00 next day, or any server not on IST. | Anchor to the R1 attendance date in IST. |
| 4 | `compute_work_hours` | `round()` is banker's/float rounding, not half-up (R4); no truncation to whole seconds. | A 0.005 h boundary (18 s) rounds the wrong way. | Integer half-up on seconds: `(secs*200 + 3600)//7200`. |
| 5 | `compute_overtime` | No 30-minute minimum (R3); builds `end` from a naive `fromisoformat` and subtracts from the punch, ignores overnight shifts (end is next day). | 5 minutes past shift end counts as overtime; overnight shift gives negative/zero overtime. | Rewritten per R3, with next-day end for overnight shifts. |
| 6 | `EmployeeIn` | No validation: `emp_code` format, e-mail, `HH:MM`, real `joined_on` date, `shift_start != shift_end`. | POST with `shift_start="25:99"` returns 201. | Pydantic patterns and validators. |
| 7 | `create_employee` | check-then-insert and no unique index; concurrent creates make duplicates. | Fire two identical POSTs at once. | Unique index on `emp_code`; catch `DuplicateKeyError` -> 409. |
| 8 | `create_employee` | `created_at = datetime.now()` is naive local time and is returned as an ISO string, not epoch ms. | `created_at` is a string in the response. | UTC now, serialised as epoch ms. |
| 9 | `list_employees` | `skip = page * page_size` skips the whole first page. | Page 1 starts at item 21. | `(page - 1) * page_size`. |
| 10 | `list_employees` | `total` ignores the `department` filter; no sort; `page`/`page_size` unvalidated (0, negative, huge). | `total` larger than filtered items; `page=0` gives a Mongo error (500). | Filtered count, `sort emp_code`, `Query(ge=1, le=100)`. |
| 11 | `punch_in` | Unknown employee: `emp` is `None` -> `TypeError` -> 500. | POST with a bad `emp_code`. | 404. |
| 12 | `punch_in` | `punched_at / 1000` uses local naive time, float division, no range check; `status` accepts `ABSENT`/garbage. | Seconds-looking value accepted; `status="ABSENT"` creates a punch-in. | Strict epoch-ms type, truncation to seconds, `PresenceStatus` literal. |
| 13 | `punch_in` | `date` taken from the local timestamp, ignoring IST and R1 overnight rule. | Overnight punch-in after midnight lands on the wrong day. | `attendance_date()`. |
| 14 | `punch_in` | check-then-insert race; no unique `(emp_code, date)`. | Two simultaneous punch-ins both return 201. | Unique index + `DuplicateKeyError` -> 409. |
| 15 | `punch_in` | Response leaks `id` (ObjectId string) and returns raw datetimes. | `id` present, `punch_in` is a string. | Shared `record_out()` serializer: no `_id`, epoch ms. |
| 16 | `list_attendance` | Loads the whole collection, sorts and paginates in Python; no secondary sort. | Slow/high memory on 100k records; unstable order for equal dates. | Server-side `sort(date desc, emp_code asc)` + `skip/limit` on an index; `count_documents` for `total`. |
| 17 | `list_attendance` | `date_from/date_to/status` unvalidated; `from > to` not rejected; `page=0` gives a negative slice; leaks `id`; missing legacy fields not defaulted. | `date_from=banana`; `page=0`. | Validation (422), `Query` bounds, `record_out()` defaults. |
| 18 | `/health` | Always "ok", even if MongoDB is down. | Stop Mongo; health still 200. | Pings Mongo, 503 on failure. |
| 19 | startup | No indexes at all. | `explain` shows `COLLSCAN`. | Idempotent `ensure_indexes()` at startup. |
| 20 | missing endpoints | punch-out, PATCH, analytics, explain not implemented. | 404/405. | Implemented (analytics + explain in stage 2). |

## Looked at and decided NOT a defect
- `doc.pop("_id", None)` after `insert_one`: correct, `insert_one` mutates the dict by adding `_id`.
- `IST = timezone(timedelta(hours=5, minutes=30))`: correct, India has no DST.
- `load_dotenv()`: default `override=False`, so real environment variables already win.
- `db.employees.find_one({"emp_code": ...})` for lookups: fine once `emp_code` is uniquely indexed.