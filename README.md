# Employee Attendance & Analytics API

FastAPI + MongoDB (sync PyMongo). All code is in `app/main.py`.

## Run
```
pip install -r requirements.txt
export MONGO_URI="mongodb+srv://<user>:<password>@<cluster>/"   # or put it in a local .env
export MONGO_DB=attendance_db
uvicorn app.main:app --port 8000
```
Indexes are created idempotently at startup (and retried from `/health`). Developed against MongoDB 8.0
(`$densify` needs 5.1+, `$setWindowFields`/`$rank` need 5.0+).

## Notes
- Instants are stored as UTC BSON dates and exposed as epoch milliseconds; calendar dates and shift times stay strings.
- Concurrency relies on database guarantees: unique indexes (employee, punch-in), compare-and-set updates (punch-out, regularize).
- Analytics read the stored derived values. Reported numbers are rounded half-up inside the pipelines using integer arithmetic.
- `/admin/explain/{endpoint}` explains the same pipeline/query object the endpoint runs.

## Interpretations
- A `PATCH` that leaves the recomputed record identical to the stored one is 422.
- Punch times sent for an `ABSENT`/`LEAVE` record without a presence status are 422.
- `moving_avg_7d` averages the already-rounded (4 dp) daily rates.