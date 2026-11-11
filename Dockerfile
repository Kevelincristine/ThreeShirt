FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RENDER=true

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY threeshirt/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY threeshirt/ ./

RUN useradd --create-home --uid 10001 threeshirt \
    && mkdir -p /app/staticfiles /app/backups/maintenance \
    && chown -R threeshirt:threeshirt /app

USER threeshirt

CMD ["sh", "-c", "python manage.py collectstatic --noinput && exec gunicorn backend.wsgi:application --bind 0.0.0.0:${PORT:-10000} --workers 2 --timeout 120"]
