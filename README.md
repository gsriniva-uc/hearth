# Hearth 🏠 — Family OS

> *The family intelligence layer. So nothing falls through the cracks.*

AI-powered household planner built on the same multi-agent backbone as Alterus.  
MVP: **Kids Calendar + Nudge Engine** — school events, early dismissals, recitals, field trips, and more, with smart reminders fired to Outlook via Power Automate.

---

## Quick Start (Local)

```bash
git clone https://github.com/YOUR_USERNAME/hearth
cd hearth
cp .env.example .env        # fill in your values
pip install -r requirements.txt
streamlit run ui/app.py
```

In a second terminal:
```bash
uvicorn scheduler.nudge_scheduler:app --reload
```

---

## Deploy to Render

1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → New → Blueprint
3. Select this repo — Render reads `render.yaml` and creates both services automatically
4. Set environment variables in the Render dashboard (see `.env.example`)

---

## Environment Variables

See `.env.example` for all values. Minimum required:

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | From [console.anthropic.com](https://console.anthropic.com) |
| `CHILDREN` | Comma-separated names, e.g. `Avery,Jordan` |
| `PARENT_EMAIL` | Where nudges are delivered |
| `POWER_AUTOMATE_WEBHOOK` | HTTP trigger URL from your Power Automate flow |

---

## Architecture

```
supervisor
  └── intake_agent          classify input (pdf / manual / nl_command / query)
        ├── event_parser    PDF newsletter → structured events (Claude)
        └── calendar_agent  CRUD on SQLite events.db

scheduler/nudge_scheduler   APScheduler cron (daily 7am)
  └── channels/dispatcher   POST to Power Automate → Outlook
```

---

## Sprint Plan

| Sprint | Goal |
|---|---|
| 1 | SQLite schema + calendar CRUD + Streamlit event list |
| 2 | Intake agent + PDF newsletter parser |
| 3 | Nudge scheduler + dispatcher + webhook wiring |
| V2 | Financial agent — bank statement OCR, savings/portfolio suggestions |

---

## Stack

- **LLM** Claude (Anthropic API)
- **Agent framework** LangGraph + LangChain
- **Vector memory** ChromaDB (family preferences, history)
- **DB** SQLite (events)
- **Backend** FastAPI + APScheduler
- **Frontend** Streamlit
- **Notifications** Power Automate → Outlook
- **Observability** LangSmith
- **Deploy** Render / Docker
