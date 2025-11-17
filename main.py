import os
from typing import List, Optional, Any, Dict
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from bson import ObjectId

from database import db, create_document, get_documents
from schemas import Property as PropertySchema, Booking as BookingSchema

# Optional Google Calendar (service account)
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

app = FastAPI(title="Robinsons Land Property Listings API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    except Exception as e:
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
    return response


# ---------------------- Schemas (for viewers) ----------------------
@app.get("/schema")
def get_schema():
    return {
        "property": PropertySchema.model_json_schema(),
        "booking": BookingSchema.model_json_schema(),
    }


# ---------------------- Properties CRUD ----------------------
@app.get("/api/properties")
def list_properties():
    docs = get_documents("property")
    return [to_str_id(d) for d in docs]


@app.post("/api/properties")
def create_property(payload: PropertySchema):
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
def update_property(prop_id: str, payload: PropertySchema):
    oid = get_object_id(prop_id)
    data = payload.model_dump()
    data["updated_at"] = datetime.now(timezone.utc)
    res = db["property"].update_one({"_id": oid}, {"$set": data})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Property not found")
    doc = db["property"].find_one({"_id": oid})
    return to_str_id(doc)


@app.delete("/api/properties/{prop_id}")
def delete_property(prop_id: str):
    oid = get_object_id(prop_id)
    res = db["property"].delete_one({"_id": oid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Property not found")
    return {"ok": True}


# ---------------------- Bookings / Inquiries ----------------------
class BookingCreate(BookingSchema):
    pass


@app.get("/api/bookings")
def list_bookings():
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
        description = f"Inquiry for property at {prop.get('location', '')}\nName: {payload.name}\nEmail: {payload.email}\nPhone: {payload.phone or ''}\nMessage: {payload.message or ''}"
        event_id = create_calendar_event(summary, description, start_dt)
    if event_id:
        data["google_event_id"] = event_id

    inserted_id = create_document("booking", data)
    doc = db["booking"].find_one({"_id": ObjectId(inserted_id)})
    return to_str_id(doc)


class BookingUpdate(BaseModel):
    status: Optional[str] = None
    message: Optional[str] = None
    preferred_datetime: Optional[datetime] = None


@app.patch("/api/bookings/{booking_id}")
def update_booking(booking_id: str, payload: BookingUpdate):
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
def delete_booking(booking_id: str):
    oid = get_object_id(booking_id)
    res = db["booking"].delete_one({"_id": oid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Booking not found")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
