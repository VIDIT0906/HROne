# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, duplicate-create guard); `(department, emp_code)` (sorted department listing); `(department, joined_on, emp_code)` (headcount, answered from the index alone). `attendance_logs`: unique `(emp_code, date)` (one record per day, punch-in race, monthly and join lookups); `(date desc, emp_code, late_minutes)` (list order, leaderboard); `(status, date desc, emp_code)` (status filter). Rejected: a separate `(emp_code, date desc)`, because the unique index read backwards gives that order.

2. **Punch-in race.** Both requests find the employee and compute the same date, then both insert. The unique `(emp_code, date)` index lets MongoDB accept one; the other gets a duplicate-key error and returns 409. No check-then-insert.

3. **Ties.** Tied employees share a rank and the next rank is skipped. I cut at `rank <= limit`, so everyone tied at the cutoff is returned, even if that exceeds `limit`.

4. **Headcount.** The summary starts from employees who joined by month end, grouped by department, and only then joins logs, so people with no logs still count.

5. **100x.** The summary pushes every employee code of a department into one document before joining logs. I'd keep monthly per-employee rollups, updated on punch and correction.