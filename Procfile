web: streamlit run ui/app.py --server.port $PORT --server.address 0.0.0.0
scheduler: uvicorn scheduler.nudge_scheduler:app --host 0.0.0.0 --port ${API_PORT:-8000}
