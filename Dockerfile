FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dail ./dail
COPY tests ./tests
COPY README.md .
COPY AGENT_QUICKSTART.md .
EXPOSE 8000
CMD ["sh", "-c", "uvicorn dail.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
