# Smart Hospital Queue Bot

AI-powered hospital queue bot that tells patients **when to arrive** instead of making them wait.

Watches a live queue → predicts each patient's start time → LLM drafts a personalized SMS → **operator approves or edits it** → message sent. If the patient doesn't show, an AI voice call is made once.

```
Hospital live data ──▶ Backend ──▶ LLM API
                          │
                   Bot asks for confirmation
                          │
                          ▼
              Operator dashboard ──▶ Approve / Edit ──▶ SMS or Voice Call
```

---

## Requirements

Before running, install:

- **Python 3.10 or higher** — https://www.python.org/downloads/ (Windows: tick **"Add Python to PATH"**)
- **pip** — bundled with Python
- **A modern web browser** — Chrome, Edge, Firefox, or Safari
- **Git** *(optional)* — only if cloning the repo

No database server needed — SQLite is bundled with Python.

---

## API

The bot works with **any OpenAI-compatible API**. Recommended: **Groq** (free, fast).

Get a key at **https://console.groq.com/keys** and paste it in `.env`:

```env
LLM_API_KEY=gsk_your_groq_key_here
LLM_API_URL=https://api.groq.com/openai/v1/chat/completions
LLM_MODEL=llama-3.3-70b-versatile
```

Alternatives:

- **OpenAI** → `https://api.openai.com/v1/chat/completions`, model `gpt-4o-mini`
- **Ollama (local)** → `http://localhost:11434/v1/chat/completions`, model `llama3.1`
- **No API** → leave `LLM_API_KEY` empty; the bot runs in template mode

---

## Quick start

```bash
git clone https://github.com/MakersNeedMore-MnM/Round2-kd8830.git
cd Round2-kd8830

python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env                # Windows: copy .env.example .env
# edit .env and paste your Groq key

uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000**.

---

## How it works

1. Patient books an appointment → token issued
2. System tracks real-time queue and predicts wait time
3. AI drafts a personalized notification
4. Staff reviews and approves or edits it
5. Patient receives an SMS (or voice call for no-shows)
6. System learns from actual times and improves future predictions

Nothing reaches a patient until an operator approves it.

---

## Tech stack

FastAPI · SQLite · httpx · Groq (LLM) · WebSocket · Vanilla JS · Optional Twilio for real SMS/voice

## License
