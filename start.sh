#!/bin/sh
# Start FastAPI webhook server in background, then Streamlit in foreground.
uvicorn scheduler.nudge_scheduler:app --host 0.0.0.0 --port ${WEBHOOK_PORT:-8000} &
streamlit run ui/app.py --server.port 8501 --server.address 0.0.0.0
