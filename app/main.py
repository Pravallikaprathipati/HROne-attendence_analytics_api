"""
Employee Attendance & Analytics API

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
STATUS_VALUES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
PRESENCE_VALUES = {"PRESENT", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="joined_dept")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_date")
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)], name="date_emp_code")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)], name="emp_punch_in")
    db.attendance_logs.create_index([("status", 1), ("date", 1)], name="status_date")


@app.on_event("startup")
def startup_events():
    ensure_indexes()


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_times(cls, value: str) -> str:
        if value == "":
            raise ValueError("shift time cannot be empty")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENCE_VALUES:
            raise ValueError("status must be one of PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in STATUS_VALUES:
            raise ValueError("status is invalid")
        return value


def ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_datetime_utc(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, bool) or type(value) is not int:
        raise ValueError("epoch milliseconds must be an integer")
    if value < 100000000000 or value > 4102444800000:
        raise ValueError("epoch milliseconds out of range")
    dt = datetime.fromtimestamp(value / 1000, tz=UTC)
    return dt.replace(microsecond=0)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    dt = ensure_utc(value)
    return int(dt.astimezone(UTC).timestamp() * 1000)


def is_working_day(day: date) -> bool:
    return day.weekday() < 5


def round_decimal(value: Any, digits: int) -> Decimal:
    if value is None:
        return Decimal("0")
    quant = Decimal("1").scaleb(-digits)
    return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)


def round_float(value: Any, digits: int) -> float:
    return float(round_decimal(value, digits))


def parse_shift_time(value: str) -> time:
    return time.fromisoformat(value)


def validate_date_arg(value: Optional[str], field_name: str) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD") from exc


def date_from_iso(value: str) -> date:
    return date.fromisoformat(value)


def attendance_date_for_punch_in(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    punch_in = ensure_utc(punch_in)
    punch_in_ist = punch_in.astimezone(IST)
    start_time = parse_shift_time(shift_start)
    end_time = parse_shift_time(shift_end)
    if end_time <= start_time:
        if punch_in_ist.timetz() < end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
        return punch_in_ist.date().isoformat()
    return punch_in_ist.date().isoformat()


def shift_start_dt_for_record(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    rec_date = date_from_iso(attendance_date)
    return datetime.combine(rec_date, parse_shift_time(shift_start), tzinfo=IST)


def shift_end_dt_for_record(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    rec_date = date_from_iso(attendance_date)
    end_time = parse_shift_time(shift_end)
    start_time = parse_shift_time(shift_start)
    if end_time <= start_time:
        return datetime.combine(rec_date + timedelta(days=1), end_time, tzinfo=IST)
    return datetime.combine(rec_date, end_time, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    punch_in = ensure_utc(punch_in)
    ist_dt = punch_in.astimezone(IST)
    record_date = attendance_date_for_punch_in(punch_in, shift_start, shift_end)
    start_dt = shift_start_dt_for_record(record_date, shift_start, shift_end)
    diff_minutes = int((ist_dt - start_dt).total_seconds() / 60)
    return diff_minutes if diff_minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    total_seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(total_seconds)) / Decimal("3600")
    return float(round_decimal(hours, 2))


def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str) -> int:
    punch_out = ensure_utc(punch_out)
    end_dt = shift_end_dt_for_record(attendance_date, "00:00", shift_end)
    delta_minutes = int((punch_out.astimezone(IST) - end_dt).total_seconds() / 60)
    return max(0, delta_minutes) if delta_minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    rounded = round_decimal(work_hours, 2)
    return rounded < Decimal("4.50")


def serialize_epoch_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_epoch_ms(value)
    return value


def serialize_history_entry(entry: dict) -> dict:
    payload: dict[str, Any] = {
        "at": serialize_epoch_value(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {},
    }
    for key, value in (entry.get("changes") or {}).items():
        payload["changes"][key] = {
            "from": serialize_epoch_value(value.get("from")) if key in {"punch_in", "punch_out"} else value.get("from"),
            "to": serialize_epoch_value(value.get("to")) if key in {"punch_in", "punch_out"} else value.get("to"),
        }
    return payload


def serialize_record(doc: dict) -> dict:
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": None if doc.get("work_hours") is None else float(doc.get("work_hours")),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(item) for item in (doc.get("history") or [])],
    }


def serialize_employee(doc: dict) -> dict:
    payload = {k: doc.get(k) for k in ["emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on"]}
    payload["created_at"] = to_epoch_ms(doc.get("created_at"))
    return payload


def build_attendance_filter(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def ensure_employee_exists(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


def parse_epoch_or_422(value: Optional[int], field_name: str) -> Optional[datetime]:
    try:
        return to_datetime_utc(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} is invalid") from exc


@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception as exc:  # pragma: no cover - handled by runtime
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    try:
        doc = body.model_dump()
        doc["created_at"] = datetime.now(UTC)
        result = db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    created = db.employees.find_one({"_id": result.inserted_id})
    return serialize_employee(created)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    for item in items:
        item["created_at"] = to_epoch_ms(item.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = ensure_employee_exists(body.emp_code)
    punched_at = datetime.now(UTC).replace(microsecond=0) if body.punched_at is None else parse_epoch_or_422(body.punched_at, "punched_at")
    if punched_at is None:
        punched_at = datetime.now(UTC).replace(microsecond=0)
    date_value = attendance_date_for_punch_in(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_value,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = ensure_employee_exists(body.emp_code)
    punched_at = datetime.now(UTC).replace(microsecond=0) if body.punched_at is None else parse_epoch_or_422(body.punched_at, "punched_at")
    if punched_at is None:
        punched_at = datetime.now(UTC).replace(microsecond=0)
    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "punch_in": {"$ne": None, "$lte": punched_at},
    }, sort=[("punch_in", -1)])
    if record is None:
        raise HTTPException(status_code=404, detail="punch-in not found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")
    punch_in_dt = ensure_utc(record["punch_in"])
    if punched_at <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    elapsed_seconds = (punched_at - punch_in_dt).total_seconds()
    if elapsed_seconds > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
    work_hours = compute_work_hours(punch_in_dt, punched_at)
    overtime = compute_overtime(punched_at, emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)
    update = {
        "punch_out": punched_at,
        "work_hours": round_float(work_hours, 2),
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="record already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    query = build_attendance_filter(emp_code=emp_code, date_from=date_from, date_to=date_to, status=status)
    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    items = list(db.attendance_logs.find(query, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size))
    for item in items:
        item["punch_in"] = to_epoch_ms(item.get("punch_in"))
        item["punch_out"] = to_epoch_ms(item.get("punch_out"))
        item["history"] = [serialize_history_entry(entry) for entry in (item.get("history") or [])]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    validate_date_arg(date, "date")
    emp = ensure_employee_exists(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status_code=422, detail="request changes nothing")

    new_status = body.status if body.status is not None else record.get("status")
    if new_status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot include punch times")

    new_punch_in = ensure_utc(record.get("punch_in"))
    if body.punch_in is not None:
        new_punch_in = parse_epoch_or_422(body.punch_in, "punch_in")
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="invalid punch_in")
        if attendance_date_for_punch_in(new_punch_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(status_code=422, detail="punch_in must match attendance date")

    new_punch_out = ensure_utc(record.get("punch_out"))
    if body.punch_out is not None:
        new_punch_out = parse_epoch_or_422(body.punch_out, "punch_out")
        if new_punch_out is None:
            raise HTTPException(status_code=422, detail="invalid punch_out")

    if new_status in PRESENCE_VALUES and new_punch_in is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")

    if new_punch_in is not None and new_punch_out is not None:
        if new_punch_out <= new_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    if new_status in {"ABSENT", "LEAVE"}:
        new_punch_in = None
        new_punch_out = None

    new_late_minutes = 0 if new_punch_in is None else compute_late_minutes(new_punch_in, emp["shift_start"], emp["shift_end"])
    if new_punch_in is None or new_punch_out is None:
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
        new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_end"], date)
        new_half_day = compute_half_day(new_work_hours)

    old_status = record.get("status")
    old_punch_in = ensure_utc(record.get("punch_in"))
    old_punch_out = ensure_utc(record.get("punch_out"))
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    updated = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }
    changes: dict[str, Any] = {}
    for field, old_value, new_value in [
        ("status", old_status, new_status),
        ("punch_in", old_punch_in, new_punch_in),
        ("punch_out", old_punch_out, new_punch_out),
        ("work_hours", old_work_hours, new_work_hours),
        ("late_minutes", old_late_minutes, new_late_minutes),
        ("overtime_minutes", old_overtime_minutes, new_overtime_minutes),
        ("half_day", old_half_day, new_half_day),
    ]:
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {k: {"from": to_epoch_ms(v["from"]) if k in {"punch_in", "punch_out"} and isinstance(v["from"], datetime) else v["from"], "to": to_epoch_ms(v["to"]) if k in {"punch_in", "punch_out"} and isinstance(v["to"], datetime) else v["to"]} for k, v in changes.items()},
    }
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": record.get("history", [])},
        {"$set": updated, "$push": {"history": entry}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="concurrent update detected")
    updated_doc = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_record(updated_doc)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = ensure_employee_exists(emp_code)
    start_date = date.fromisoformat(f"{month}-01")
    end_date = (start_date.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    working_days = 0
    current = start_date
    while current <= end_date:
        if is_working_day(current):
            working_days += 1
        current += timedelta(days=1)
    if date.fromisoformat(employee["joined_on"]) > end_date:
        working_days = 0
    if date.fromisoformat(employee["joined_on"]) > start_date:
        start_count = date.fromisoformat(employee["joined_on"])
        current = start_count
        count = 0
        while current <= end_date:
            if is_working_day(current):
                count += 1
            current += timedelta(days=1)
        working_days = count
    docs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}))
    present_days = Decimal("0")
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    for doc in docs:
        if doc.get("status") == "LEAVE":
            leave_days += 1
        if doc.get("status") in PRESENCE_VALUES and is_working_day(date_from_iso(doc["date"])):
            present_days += Decimal("0.5") if bool(doc.get("half_day")) else Decimal("1")
        if doc.get("late_minutes", 0) > 0:
            late_count += 1
            total_late_minutes += int(doc.get("late_minutes", 0) or 0)
        total_overtime_minutes += int(doc.get("overtime_minutes", 0) or 0)

    attendance_pct = None if working_days == 0 else float(round_decimal((present_days / Decimal(working_days)) * Decimal("100"), 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start_date = date.fromisoformat(f"{month}-01")
    end_date = (start_date.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    end_str = end_date.isoformat()
    dept_filter: dict[str, Any] = {"joined_on": {"$lte": end_str}}
    if department is not None:
        dept_filter["department"] = department
    employees = list(db.employees.find(dept_filter, {"_id": 0, "emp_code": 1, "department": 1}))
    departments = {}
    for emp in employees:
        departments.setdefault(emp["department"], [])
        departments[emp["department"]].append(emp["emp_code"])
    results = []
    month_prefix = f"{month}-"
    for dept_name in sorted(departments):
        emp_codes = departments[dept_name]
        headcount = len(emp_codes)
        logs = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$regex": f"^{month_prefix}"}}))
        present_days = Decimal("0")
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0
        work_hours_values = []
        for doc in logs:
            if doc.get("status") in PRESENCE_VALUES and is_working_day(date_from_iso(doc["date"])):
                present_days += Decimal("0.5") if bool(doc.get("half_day")) else Decimal("1")
            if doc.get("late_minutes", 0) > 0:
                late_count += 1
                total_late_minutes += int(doc.get("late_minutes", 0) or 0)
            if doc.get("status") == "LEAVE":
                leave_count += 1
            if doc.get("status") == "ON_DUTY":
                on_duty_count += 1
            if doc.get("status") in PRESENCE_VALUES and doc.get("work_hours") is not None:
                work_hours_values.append(float(doc.get("work_hours")))
        avg_work_hours = None if not work_hours_values else float(round_decimal(sum(work_hours_values) / len(work_hours_values), 2))
        results.append({
            "department": dept_name,
            "headcount": headcount,
            "present_days": float(present_days),
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })
    return {"month": month, "items": results}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    employee_filter: dict[str, Any] = {}
    if department is not None:
        employee_filter["department"] = department
    employees = list(db.employees.find(employee_filter, {"_id": 0, "emp_code": 1, "name": 1, "department": 1}))
    emp_lookup = {item["emp_code"]: item for item in employees}
    logs = list(db.attendance_logs.find({"emp_code": {"$in": list(emp_lookup.keys())}, "date": {"$regex": f"^{month}-"}}))
    totals: dict[str, dict[str, Any]] = {}
    for log in logs:
        if log.get("emp_code") not in emp_lookup:
            continue
        if log.get("late_minutes", 0) <= 0:
            continue
        emp = totals.setdefault(log["emp_code"], {"emp_code": log["emp_code"], "total_late_minutes": 0, "late_count": 0})
        emp["total_late_minutes"] += int(log.get("late_minutes", 0) or 0)
        emp["late_count"] += 1
    rows = []
    for emp_code, summary in totals.items():
        info = emp_lookup.get(emp_code, {})
        rows.append({
            "emp_code": emp_code,
            "name": info.get("name"),
            "department": info.get("department"),
            "total_late_minutes": summary["total_late_minutes"],
            "late_count": summary["late_count"],
        })
    rows.sort(key=lambda row: (-row["total_late_minutes"], row["emp_code"]))
    ranked_rows = []
    rank = 0
    prev_total = None
    for row in rows:
        if prev_total is None or row["total_late_minutes"] != prev_total:
            rank = len(ranked_rows) + 1
            prev_total = row["total_late_minutes"]
        row["rank"] = rank
        ranked_rows.append(row)
    final_rows = [
        {
            "rank": row["rank"],
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        }
        for row in ranked_rows if row["rank"] <= limit
    ]
    return {"month": month, "items": final_rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    validate_date_arg(from_date, "from")
    validate_date_arg(to_date, "to")
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be >= from")
    start = date_from_iso(from_date)
    end = date_from_iso(to_date)
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range exceeds 92 days")
    employees = list(db.employees.find({"department": department, "joined_on": {"$lte": to_date}}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(status_code=404, detail="department not found")
    emp_codes = [e["emp_code"] for e in employees]
    logs = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}}))
    log_map: dict[str, dict[str, Any]] = {log["emp_code"]: log for log in logs}
    daily = []
    current = start
    while current <= end:
        day_key = current.isoformat()
        date_logs = [log for log in logs if log["date"] == day_key]
        headcount = sum(1 for emp in employees if emp["joined_on"] <= day_key)
        present_count = sum((Decimal("0.5") if bool(log.get("half_day")) else Decimal("1")) for log in date_logs if log.get("status") in PRESENCE_VALUES)
        late_count = sum(1 for log in date_logs if log.get("late_minutes", 0) > 0)
        if is_working_day(current) and headcount > 0:
            attendance_rate = float(round_decimal((present_count / Decimal(headcount)), 4))
        else:
            attendance_rate = None
        if not is_working_day(current):
            attendance_rate = None
        daily.append({
            "date": day_key,
            "is_working_day": is_working_day(current),
            "headcount": headcount,
            "present_count": float(present_count),
            "late_count": late_count,
            "attendance_rate": attendance_rate,
        })
        current += timedelta(days=1)

    moving_history = []
    for row in daily:
        values = []
        for prev in daily[max(0, daily.index(row) - 6):daily.index(row) + 1]:
            if prev["attendance_rate"] is not None:
                values.append(prev["attendance_rate"])
        moving_avg = None if not values else float(round_decimal(sum(values) / len(values), 4))
        row["moving_avg_7d"] = moving_avg
        moving_history.append(row)
    return {"department": department, "items": [{
        "date": row["date"],
        "is_working_day": row["is_working_day"],
        "headcount": row["headcount"],
        "present_count": row["present_count"],
        "late_count": row["late_count"],
        "attendance_rate": row["attendance_rate"],
        "moving_avg_7d": row["moving_avg_7d"],
    } for row in moving_history]}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail="unsupported endpoint")
    if endpoint == "attendance_list":
        q = build_attendance_filter(emp_code=emp_code, date_from=date_from, date_to=date_to, status=status)
        pipeline = [{"$match": q}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page - 1) * page_size}, {"$limit": page_size}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}}, {"$sort": {"date": 1}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start = date.fromisoformat(f"{month}-01")
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        pipeline = [{"$match": {"joined_on": {"$lte": end.isoformat()}}}]
        if department:
            pipeline[0]["$match"]["department"] = department
        pipeline.extend([
            {"$lookup": {"from": "attendance_logs", "let": {"emp_code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$eq": ["$emp_code", "$$emp_code"]}, "date": {"$regex": f"^{month}-"}}}], "as": "logs"}},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ])
        explain = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}})
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = [{"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}}, {"$sort": {"late_minutes": -1, "emp_code": 1}}]
        if department:
            pipeline = [{"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}}, {"$match": {"$and": [{"date": {"$regex": f"^{month}-"}}, {"late_minutes": {"$gt": 0}}, {"emp.department": department}]}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "department_trend":
        if department is None or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        pipeline = [
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$match": {"emp.department": department, "date": {"$gte": from_date, "$lte": to_date}}},
            {"$sort": {"date": 1, "emp_code": 1}},
        ]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    raise HTTPException(status_code=422, detail="unsupported endpoint")
