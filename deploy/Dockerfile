FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt python-docx lxml
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini main.py ./
COPY data/samples ./data/samples
RUN useradd --create-home --uid 10001 paper && mkdir -p /app/data/runs && chown -R paper:paper /app/data
USER paper
CMD ["uvicorn", "app.web.api:app", "--host", "0.0.0.0", "--port", "8000"]
