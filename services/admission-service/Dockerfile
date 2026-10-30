FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY leader_election.py ./leader_election.py
COPY app/ ./app/
    
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]