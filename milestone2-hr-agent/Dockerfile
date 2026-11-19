# Starter: lengkapi image aplikasi untuk FastAPI DAN Streamlit pada Compose.
# Build tidak boleh membutuhkan OPENAI_API_KEY; konfigurasi model diberikan saat runtime.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 appgroup \
    && useradd \
    --uid 10001 \
    --gid appgroup \
    --create-home \
    --shell /usr/sbin/nologin \
    appuser

COPY requirements.txt pyproject.toml ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY --chown=appuser:appgroup src ./src
COPY --chown=appuser:appgroup data ./data
COPY --chown=appuser:appgroup tests ./tests
COPY --chown=appuser:appgroup templates ./templates
COPY --chown=appuser:appgroup app.py ui_contract.py demo_personas.json ./

RUN mkdir -p /app/outputs \
    && chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000 8501

CMD ["python", "-m", "uvicorn", "src.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]