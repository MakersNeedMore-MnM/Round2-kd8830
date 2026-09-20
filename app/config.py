# app/config.py
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(value, default=False):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _int(value, default):
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value, default):
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _str(value, default=""):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


class Settings:
    def __init__(self):
        self.LLM_API_KEY = _str(os.getenv("LLM_API_KEY"), "")
        self.LLM_API_URL = _str(
            os.getenv("LLM_API_URL"),
            "https://api.groq.com/openai/v1/chat/completions",
        )
        self.LLM_MODEL = _str(os.getenv("LLM_MODEL"), "llama-3.3-70b-versatile")
        self.LLM_TIMEOUT = _float(os.getenv("LLM_TIMEOUT"), 20.0)

        self.HOSPITAL_NAME = _str(os.getenv("HOSPITAL_NAME"), "City General Hospital")
        self.DATABASE_PATH = _str(os.getenv("DATABASE_PATH"), "smart_queue.db")
        self.DEFAULT_CONSULT_MINUTES = _int(os.getenv("DEFAULT_CONSULT_MINUTES"), 12)
        self.NOTIFY_LEAD_MINUTES = _int(os.getenv("NOTIFY_LEAD_MINUTES"), 30)

        self.AUTO_SIMULATE = _bool(os.getenv("AUTO_SIMULATE"), True)
        self.SIMULATE_INTERVAL = _int(os.getenv("SIMULATE_INTERVAL"), 25)

        self.SMS_PROVIDER = _str(os.getenv("SMS_PROVIDER"), "console").lower()
        self.TWILIO_SID = _str(os.getenv("TWILIO_ACCOUNT_SID"), "")
        self.TWILIO_TOKEN = _str(os.getenv("TWILIO_AUTH_TOKEN"), "")
        self.TWILIO_FROM = _str(os.getenv("TWILIO_FROM_NUMBER"), "")


settings = Settings()
