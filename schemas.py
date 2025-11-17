"""
Database Schemas

Define your MongoDB collection schemas here using Pydantic models.
These schemas are used for data validation in your application.

Each Pydantic model represents a collection in your database.
Model name is converted to lowercase for the collection name:
- User -> "user" collection
- Product -> "product" collection
- BlogPost -> "blogs" collection
"""

from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List
from datetime import datetime

# -------------------- Property Listings --------------------
class Property(BaseModel):
    """
    Properties collection schema
    Collection name: "property" (lowercase of class name)
    """
    title: str = Field(..., description="Listing title")
    description: Optional[str] = Field(None, description="Detailed description")
    price: float = Field(..., ge=0, description="Listing price")
    location: str = Field(..., description="City / Address / Location")
    bedrooms: Optional[int] = Field(None, ge=0)
    bathrooms: Optional[float] = Field(None, ge=0)
    area_sqm: Optional[float] = Field(None, ge=0, description="Floor area in sqm")
    images: Optional[List[str]] = Field(default_factory=list, description="Image URLs")
    status: str = Field("available", description="available | reserved | sold")

# -------------------- Booking / Inquiry --------------------
class Booking(BaseModel):
    """
    Bookings/Inquiries collection schema
    Collection name: "booking"
    """
    property_id: str = Field(..., description="Related property _id as string")
    name: str = Field(..., description="Prospect full name")
    email: EmailStr = Field(..., description="Prospect email")
    phone: Optional[str] = Field(None, description="Prospect phone")
    message: Optional[str] = Field(None, description="Extra details")
    preferred_datetime: Optional[datetime] = Field(None, description="Requested viewing time (UTC)")
    status: str = Field("pending", description="pending | confirmed | canceled")
    google_event_id: Optional[str] = Field(None, description="Linked Google Calendar event id, if created")
