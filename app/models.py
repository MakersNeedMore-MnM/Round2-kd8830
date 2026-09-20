# app/models.py
from typing import Optional

from pydantic import BaseModel, Field


class BookingRequest(BaseModel):
    patient_name: str = Field(..., min_length=2, max_length=80)
    phone: str = Field(..., min_length=6, max_length=20)
    department: str = Field(..., min_length=2, max_length=60)
    doctor: str = Field(..., min_length=2, max_length=80)


class ApproveRequest(BaseModel):
    final_text: Optional[str] = None
    channel: Optional[str] = None  # "sms" | "voice"


class RejectRequest(BaseModel):
    reason: Optional[str] = None


class StatusChangeRequest(BaseModel):
    note: Optional[str] = None