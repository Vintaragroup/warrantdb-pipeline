from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime

class Bond(BaseModel):
    amount: Optional[float] = None
    type: Optional[str] = None
    condition: Optional[str] = None
    source_url: Optional[str] = None
    effective_at: Optional[datetime] = None

class CustodyEvent(BaseModel):
    person_id: Optional[str] = None
    county: Optional[str] = None
    facility: Optional[str] = None
    booking_number: Optional[str] = None
    status: Optional[str] = None
    booked_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    source_url: Optional[str] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    charges: List[Dict[str, Any]] = Field(default_factory=list)
    bonds: List[Bond] = Field(default_factory=list)

class Warrant(BaseModel):
    person_id: Optional[str] = None
    county: Optional[str] = None
    number: Optional[str] = None
    offense: Optional[str] = None
    status: Optional[str] = None
    issuing_agency: Optional[str] = None
    issued_date: Optional[str] = None
    bond: Optional[Bond] = None
    source_url: Optional[str] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)

class Person(BaseModel):
    full_name: str
    dob: Optional[str] = None
    aka: List[str] = Field(default_factory=list)
    identifiers: Dict[str, List[str]] = Field(default_factory=dict)
    contact: Dict[str, Any] = Field(default_factory=dict)
    links: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

def now_iso():
    return datetime.utcnow().isoformat() + "Z"
