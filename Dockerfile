FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY tutor_assistant ./tutor_assistant
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "tutor_assistant.backend:app", "--host", "0.0.0.0", "--port", "8000"]
