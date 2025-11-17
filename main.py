import os
from typing import List, Optional, Any, Dict
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from bson import ObjectId
import uuid
import smtplib
from email.mime.text import MIMEText

from database import db, create_document, get_documents
from schemas import Property as PropertySchema, Booking as BookingSchema

# Optional Google Calendar (service account)
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# Admin auth (simple token-based)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", os.getenv("SECRET_TOKEN", "supersecrettoken"))

# Email (SMTP)
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "noreply@example.com")

app = FastAPI(title="Robinsons Land Property Listings API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount uploads static directory
UPLOAD_DIR = os.path.abspath(os.getenv("UPLOAD_DIR", "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


# ---------------------- Utils ----------------------
class IdModel(BaseModel):
    id: str


def to_str_id(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return doc
    d = dict(doc)
    if "_id" in d:
        d["id"] = str(d.pop("_id"))
    # convert datetime to isoformat
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


def get_object_id(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")


# ---------------- Simple Auth Helpers ----------------
class LoginPayload(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    email: str


def require_admin(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ", 1)[1]
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return True


# ---------------- Google Calendar Helpers ----------------
_calendar_ready = False
_calendar_service = None


def _init_calendar():
    global _calendar_ready, _calendar_service
    if _calendar_ready:
        return _calendar_service
    if not GOOGLE_CALENDAR_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        _calendar_ready = True
        _calendar_service = None
        return None
    try:
        import json
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = ["https://www.googleapis.com/auth/calendar"]
        credentials = Credentials.from_service_account_info(sa_info, scopes=scopes)
        service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        _calendar_service = service
        _calendar_ready = True
        return service
    except Exception:
        # If calendar can't be initialized, we continue without it
        _calendar_ready = True
        _calendar_service = None
        return None


def create_calendar_event(summary: str, description: str, start_dt: datetime, end_dt: Optional[datetime] = None) -> Optional[str]:
    service = _init_calendar()
    if service is None:
        return None
    if end_dt is None:
        end_dt = start_dt + timedelta(hours=1)
    try:
        event_body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_dt.astimezone(timezone.utc).isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end_dt.astimezone(timezone.utc).isoformat(), "timeZone": "UTC"},
        }
        event = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event_body).execute()
        return event.get("id")
    except Exception:
        return None


# ---------------- Email Helpers ----------------
def send_email(to_email: str, subject: str, body: str) -> bool:
    if not SMTP_HOST or not to_email:
        return False
    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        server.starttls()
        if SMTP_USER and SMTP_PASS:
            server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        server.quit()
        return True
    except Exception:
        return False


# ---------------------- Root & Test ----------------------
@app.get("/")
def read_root():
    return {"message": "Robinsons Land Property Listings API"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": [],
        "google_calendar": "Not Configured",
        "email": "Not Configured",
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_name"] = getattr(db, "name", "unknown")
            response["connection_status"] = "Connected"
            try:
                response["collections"] = db.list_collection_names()
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but error: {str(e)[:80]}"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"

    # Calendar
    svc = _init_calendar()
    response["google_calendar"] = "✅ Connected" if svc else "Not Configured"
    response["calendar_id"] = GOOGLE_CALENDAR_ID or None

    # Email
    response["email"] = "✅ Configured" if SMTP_HOST else "Not Configured"
    response["smtp_host"] = SMTP_HOST

    return response


# ---------------------- Auth ----------------------
@app.post("/auth/login", response_model=LoginResponse)
def login(payload: LoginPayload):
    if payload.email == ADMIN_EMAIL and payload.password == ADMIN_PASSWORD:
        return LoginResponse(token=ADMIN_TOKEN, email=payload.email)
    raise HTTPException(status_code=401, detail="Invalid credentials")


# ---------------------- Schemas (for viewers) ----------------------
@app.get("/schema")
def get_schema():
    return {
        "property": PropertySchema.model_json_schema(),
        "booking": BookingSchema.model_json_schema(),
    }


# ---------------------- Properties CRUD + Filters ----------------------
@app.get("/api/properties")
def list_properties(
    q: Optional[str] = None,
    location: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    min_bedrooms: Optional[int] = None,
    limit: int = 100,
):
    filter_dict: Dict[str, Any] = {}
    if q:
        # search title/description
        filter_dict["$or"] = [
            {"title": {"$regex": q, "$options": "i"}},
            {"description": {"$regex": q, "$options": "i"}},
        ]
    if location:
        filter_dict["location"] = {"$regex": location, "$options": "i"}
    price_cond: Dict[str, Any] = {}
    if min_price is not None:
        price_cond["$gte"] = float(min_price)
    if max_price is not None:
        price_cond["$lte"] = float(max_price)
    if price_cond:
        filter_dict["price"] = price_cond
    if min_bedrooms is not None:
        filter_dict["bedrooms"] = {"$gte": int(min_bedrooms)}

    docs = get_documents("property", filter_dict=filter_dict, limit=limit)
    return [to_str_id(d) for d in docs]


@app.post("/api/properties")
def create_property(payload: PropertySchema, _: bool = Depends(require_admin)):
    inserted_id = create_document("property", payload)
    doc = db["property"].find_one({"_id": ObjectId(inserted_id)})
    return to_str_id(doc)


@app.get("/api/properties/{prop_id}")
def get_property(prop_id: str):
    oid = get_object_id(prop_id)
    doc = db["property"].find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Property not found")
    return to_str_id(doc)


@app.put("/api/properties/{prop_id}")
def update_property(prop_id: str, payload: PropertySchema, _: bool = Depends(require_admin)):
    oid = get_object_id(prop_id)
    data = payload.model_dump()
    data["updated_at"] = datetime.now(timezone.utc)
    res = db["property"].update_one({"_id": oid}, {"$set": data})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Property not found")
    doc = db["property"].find_one({"_id": oid})
    return to_str_id(doc)


@app.delete("/api/properties/{prop_id}")
def delete_property(prop_id: str, _: bool = Depends(require_admin)):
    oid = get_object_id(prop_id)
    res = db["property"].delete_one({"_id": oid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Property not found")
    return {"ok": True}


# ---------------------- Image Upload (Admin only) ----------------------
@app.post("/api/upload-image")
def upload_image(file: UploadFile = File(...), _: bool = Depends(require_admin)):
    ext = os.path.splitext(file.filename)[1].lower() or ".jpg"
    name = f"{uuid.uuid4().hex}{ext}"
    dest_path = os.path.join(UPLOAD_DIR, name)
    with open(dest_path, "wb") as f:
        f.write(file.file.read())
    # return accessible URL path
    return {"url": f"/uploads/{name}"}


# ---------------------- Bookings / Inquiries ----------------------
class BookingCreate(BookingSchema):
    pass


@app.get("/api/bookings")
def list_bookings(_: bool = Depends(require_admin)):
    docs = get_documents("booking")
    return [to_str_id(d) for d in docs]


@app.post("/api/bookings")
def create_booking(payload: BookingCreate):
    # Verify property exists
    try:
        prop_oid = ObjectId(payload.property_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid property_id")
    prop = db["property"].find_one({"_id": prop_oid})
    if not prop:
        raise HTTPException(status_code=404, detail="Related property not found")

    data = payload.model_dump()
    # Create calendar event if preferred time provided
    event_id = None
    if payload.preferred_datetime is not None:
        start_dt = payload.preferred_datetime
        summary = f"Viewing: {prop.get('title', 'Property')} - {payload.name}"
        description = (
            f"Inquiry for property at {prop.get('location', '')}\n"
            f"Name: {payload.name}\nEmail: {payload.email}\nPhone: {payload.phone or ''}\nMessage: {payload.message or ''}"
        )
        event_id = create_calendar_event(summary, description, start_dt)
    if event_id:
        data["google_event_id"] = event_id

    inserted_id = create_document("booking", data)
    doc = db["booking"].find_one({"_id": ObjectId(inserted_id)})

    # Send emails (best-effort)
    subject_user = "Your property inquiry has been received"
    body_user = (
        f"Hi {payload.name},\n\n"
        f"Thank you for your interest in '{prop.get('title', 'the property')}' at {prop.get('location', '')}.\n"
        + (f"We tentatively scheduled your viewing on {payload.preferred_datetime.isoformat()} (UTC).\n" if payload.preferred_datetime else "")
        + "Our team will contact you shortly.\n\nRegards,\nRobinsons Land"
    )
    send_email(payload.email, subject_user, body_user)

    if ADMIN_EMAIL:
        subject_admin = "New property inquiry"
        body_admin = (
            f"Property: {prop.get('title', '')} ({prop.get('location', '')})\n"
            f"Name: {payload.name}\nEmail: {payload.email}\nPhone: {payload.phone or ''}\n"
            f"Preferred: {payload.preferred_datetime.isoformat() if payload.preferred_datetime else 'N/A'}\n"
            f"Message: {payload.message or ''}\n"
            f"Calendar Event: {data.get('google_event_id', 'N/A')}\n"
        )
        send_email(ADMIN_EMAIL, subject_admin, body_admin)

    return to_str_id(doc)


class BookingUpdate(BaseModel):
    status: Optional[str] = None
    message: Optional[str] = None
    preferred_datetime: Optional[datetime] = None


@app.patch("/api/bookings/{booking_id}")
def update_booking(booking_id: str, payload: BookingUpdate, _: bool = Depends(require_admin)):
    oid = get_object_id(booking_id)
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        return to_str_id(db["booking"].find_one({"_id": oid}))
    updates["updated_at"] = datetime.now(timezone.utc)
    res = db["booking"].update_one({"_id": oid}, {"$set": updates})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Booking not found")
    doc = db["booking"].find_one({"_id": oid})
    return to_str_id(doc)


@app.delete("/api/bookings/{booking_id}")
def delete_booking(booking_id: str, _: bool = Depends(require_admin)):
    oid = get_object_id(booking_id)
    res = db["booking"].delete_one({"_id": oid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Booking not found")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
