# app/llm_service.py
"""Thin wrapper around any OpenAI-compatible chat-completions endpoint."""
import logging

import httpx

from .config import settings

log = logging.getLogger("llm")

SMS_SYSTEM = """You write short SMS messages for a hospital queue-management bot.

Rules:
- Maximum 320 characters. Plain text only, no markdown, no emoji.
- Friendly, calm and specific.
- Always include the patient's token number.
- Tell the patient the estimated wait and when to start travelling so they
  arrive close to their turn (arriving early is better than late).
- Never give medical advice. Never invent a doctor name or clock time that is
  not present in the supplied data.
Return ONLY the message text."""

VOICE_SYSTEM = """You write a short automated voice-call script for a hospital queue bot.

Rules:
- Maximum 45 spoken words. Plain sentences only. No markdown, no stage directions.
- Politely tell the patient that their token is now due and ask them to come to
  the desk, or to reply if they can no longer attend.
Return ONLY the script text."""


def _clean(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    return text.strip().strip('"')


def _fallback(ctx: dict) -> str:
    name = ctx["patient_name"].split()[0]
    token = ctx["token"]
    wait = ctx["wait_minutes"]
    lead = ctx["lead_minutes"]
    dept = ctx["department"]
    hospital = ctx["hospital_name"]

    if wait <= lead:
        return (
            f"Hi {name}, your token {token} at {hospital} is almost due. "
            f"Please reach the {dept} desk now."
        )
    travel_in = max(5, wait - lead)
    return (
        f"Hi {name}, token {token} for {dept} at {hospital}. "
        f"Estimated wait is about {wait} min. Please start travelling in "
        f"{travel_in} min so you arrive on time."
    )


def _voice_fallback(ctx: dict) -> str:
    name = ctx["patient_name"].split()[0]
    return (
        f"Hello {name}. This is {ctx['hospital_name']}. Your token {ctx['token']} "
        f"for {ctx['department']} is now due. Please come to the reception desk. "
        f"Thank you."
    )


async def _chat(system: str, user: str) -> str:
    payload = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
        "max_tokens": 220,
    }
    headers = {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
        r = await client.post(settings.LLM_API_URL, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        return _clean(data["choices"][0]["message"]["content"])


def _user_prompt(ctx: dict, kind: str) -> str:
    travel_in = max(5, ctx["wait_minutes"] - ctx["lead_minutes"])
    return (
        f"Hospital: {ctx['hospital_name']}\n"
        f"Patient first name: {ctx['patient_name'].split()[0]}\n"
        f"Token: {ctx['token']}\n"
        f"Department: {ctx['department']}\n"
        f"Doctor: {ctx['doctor']}\n"
        f"Patients ahead: {ctx['patients_ahead']}\n"
        f"Estimated wait from now: {ctx['wait_minutes']} minutes\n"
        f"Target arrival lead time: {ctx['lead_minutes']} minutes before turn\n"
        f"Recommended time to start travelling: {travel_in} minutes from now\n"
        f"Message type: {kind}\n\n"
        f"Write the message."
    )


async def generate_notification(ctx: dict, kind: str = "arrival") -> dict:
    fallback = _voice_fallback(ctx) if kind == "voice" else _fallback(ctx)
    if not settings.LLM_API_KEY:
        return {"text": fallback, "source": "template"}

    system = VOICE_SYSTEM if kind == "voice" else SMS_SYSTEM
    try:
        text = await _chat(system, _user_prompt(ctx, kind))
        if not text:
            raise ValueError("empty completion")
        return {"text": text, "source": "llm"}
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM call failed (%s); using template draft", exc)
        return {"text": fallback, "source": "template", "error": str(exc)}