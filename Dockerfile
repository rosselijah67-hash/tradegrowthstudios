FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY static ./static
COPY templates ./templates
COPY config ./config
COPY screenshots ./screenshots
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh
COPY README.md README_OPERATOR.md ./

RUN mkdir -p data runs artifacts screenshots public_outreach backups
RUN chmod +x ./scripts/docker-entrypoint.sh

EXPOSE 8787

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8787} --workers ${WEB_CONCURRENCY:-1} --threads ${GUNICORN_THREADS:-4} --timeout ${GUNICORN_TIMEOUT:-120} 'src.dashboard_app:create_app()'"]
