"""
Employee Attendance & Analytics API  (STAGE 1: core + attendance endpoints)
 
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)
 
Stage 2 will add: analytics endpoints and /admin/explain.
"""
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional
 
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
 
load_dotenv()
 
log = logging.getLogger("attendance")
 
# tz_aware=True: every datetime read from MongoDB is UTC-aware, never naive.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
 
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
 
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_HUNDREDTHS = 450  # R5 (4.50 h, compared on the rounded value)
MAX_SHIFT_SPAN = timedelta(hours=24)
 
# --------------------------------------------------------------------------- #
# Indexes (idempotent; created at startup, retried from /health if Mongo was
# not reachable at startup)
# --------------------------------------------------------------------------- #
_indexes_ready = False
 
 
def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    # employees
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # attendance_logs
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    _indexes_ready = True
 
 
@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the app up; /health reports 503 and retries
        log.warning("index creation deferred: %s", exc)
    yield
 
 
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)
 
 
# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
 
 
def unprocessable(loc: list, msg: str):
    """Raise a 422 with FastAPI's default body shape ({"detail": [ ... ]})."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])
 
 
def parse_date(value: str, loc: list) -> Date:
    if not DATE_RE.match(value):
        unprocessable(loc, "must be a date in YYYY-MM-DD format")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date")
 
 
# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)
 
 
def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
 
 
def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC datetime -> epoch ms using exact integer arithmetic."""
    if dt is None:
        return None
    return (aware(dt) - EPOCH) // timedelta(milliseconds=1)
 
 
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
 
 
def hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)
 
 
def at_ist(d: Date, hhmm_str: str) -> datetime:
    h, m = hhmm(hhmm_str)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
 
 
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text
 
 
# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of punch_in; for an overnight shift, a punch-in whose
    IST time is earlier than shift_end belongs to the previous day's shift."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end):
        eh, em = hhmm(shift_end)
        if (local.hour, local.minute, local.second) < (eh, em, 0):
            d -= timedelta(days=1)
    return d
 
 
def compute_late_minutes(punch_in: datetime, d: Date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then floor minutes
    since shift_start (not since the end of the grace period)."""
    secs = int((punch_in - at_ist(d, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0
 
 
def compute_overtime(punch_out: datetime, d: Date, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift_end, counted only when >= 30."""
    end = at_ist(d, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0
 
 
def work_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    """R4 in exact integer arithmetic: round-half-up(seconds / 3600, 2 dp) * 100."""
    secs = int((punch_out - punch_in).total_seconds())
    return (secs * 200 + 3600) // 7200
 
 
def derive(emp: dict, status: str, d: Date, p_in: Optional[datetime], p_out: Optional[datetime]) -> dict:
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in PRESENCE and p_in is not None:
        out["late_minutes"] = compute_late_minutes(p_in, d, emp["shift_start"])
        if p_out is not None:
            h100 = work_hundredths(p_in, p_out)
            out["work_hours"] = h100 / 100
            out["half_day"] = h100 < HALF_DAY_HUNDREDTHS
            out["overtime_minutes"] = compute_overtime(p_out, d, emp["shift_start"], emp["shift_end"])
    return out
 
 
# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
# strict=True: floats, strings and bools are rejected (422), not coerced.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
 
 
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
 
    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        Date.fromisoformat(v)  # ValueError -> 422
        return v
 
    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self
 
 
# Optional fields below are typed non-nullable with a None default on purpose:
# omitted -> None, but an explicit JSON null is rejected (the contract is not nullable).
class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None  # type: ignore[assignment]
    status: PresenceStatus = "PRESENT"
 
 
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None  # type: ignore[assignment]
 
 
class RegularizeIn(BaseModel):
    status: Status = None  # type: ignore[assignment]
    punch_in: EpochMs = None  # type: ignore[assignment]
    punch_out: EpochMs = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
 
 
# --------------------------------------------------------------------------- #
# Serialisers (Mongo document -> API shape: no _id, instants as epoch ms)
# --------------------------------------------------------------------------- #
def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": dt_to_ms(d.get("created_at")),
    }
 
 
def _hist_val(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v
 
 
def record_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [
            {
                "at": dt_to_ms(h["at"]),
                "by": h["by"],
                "reason": h["reason"],
                "changes": {
                    k: {"from": _hist_val(c.get("from")), "to": _hist_val(c.get("to"))}
                    for k, c in (h.get("changes") or {}).items()
                },
            }
            for h in (d.get("history") or [])
        ],
    }
 
 
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp
 
 
# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}
 
 
# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # uniqueness enforced by uq_emp_code, not by a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)
 
 
@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cur = (
        db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}
 
 
# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, body.status, d, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # uq_emp_date makes exactly one concurrent insert win
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)
 
 
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in <= punched_at. Works across midnight because it
    # looks at the instant, not the calendar date. Null punch_in (ABSENT/LEAVE) never matches $lte.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = aware(rec["punch_in"])
    if ts <= p_in or ts - p_in > MAX_SHIFT_SPAN:
        unprocessable(["body", "punched_at"], "must be after punch_in and within 24 hours of it")
 
    d = Date.fromisoformat(rec["date"])
    fields = {"punch_out": ts, **derive(emp, rec["status"], d, p_in, ts)}
    # Compare-and-set: only the request that still sees punch_out == null wins.
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    return record_out({**rec, **fields})
 
 
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    d_from = parse_date(date_from, ["query", "date_from"]) if date_from is not None else None
    d_to = parse_date(date_to, ["query", "date_to"]) if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        unprocessable(["query", "date_from"], "date_from must not be after date_to")
    rng = {}
    if d_from:
        rng["$gte"] = d_from.isoformat()
    if d_to:
        rng["$lte"] = d_to.isoformat()
    if rng:
        q["date"] = rng
    if status is not None:
        q["status"] = status
 
    total = db.attendance_logs.count_documents(q)
    cur = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}
 
 
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    d = parse_date(date, ["path", "date"])
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": d.isoformat()})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")
 
    old_status = rec["status"]
    old_in, old_out = aware(rec.get("punch_in")), aware(rec.get("punch_out"))
    status = body.status or old_status
 
    if status in PRESENCE:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            unprocessable(["body", "punch_in"], "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != d:
            unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT_SPAN):
            unprocessable(["body", "punch_out"], "must be after punch_in and within 24 hours of it")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(["body"], f"punch times cannot be supplied for status {status}")
        new_in = new_out = None
 
    fields = {"status": status, "punch_in": new_in, "punch_out": new_out, **derive(emp, status, d, new_in, new_out)}
    before = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": before[k], "to": v} for k, v in fields.items() if v != before[k]}
    if not changes:
        unprocessable(["body"], "request changes nothing")
 
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    raw_hist = rec.get("history")
    hist_cond = {"$size": len(raw_hist)} if isinstance(raw_hist, list) else {"$exists": False}
    # Optimistic concurrency: the write applies only if history length and every input we
    # computed from are unchanged, so two concurrent edits can never overwrite each other's entry.
    res = db.attendance_logs.update_one(
        {
            "_id": rec["_id"],
            "history": hist_cond,
            "status": old_status,
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
        },
        {"$set": fields, "$push": {"history": entry}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out({**rec, **fields, "history": (raw_hist or []) + [entry]})


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
