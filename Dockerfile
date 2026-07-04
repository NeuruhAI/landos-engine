# LandOS Engine — container image for durable hosting (Fly.io / Render / any OCI host).
# Stateless web service: connects OUT to Supabase (DATABASE_URL) + serves the REST API.
# All secrets arrive via the host's env (NOT baked in) — see DEPLOY.md.
FROM python:3.12-slim

WORKDIR /app

# psycopg[binary] ships manylinux wheels, so no libpq/build-essential needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The API only needs the `app` package (scripts/tests are batch/dev-only).
COPY app ./app

# Fly/Render inject PORT; default 8080 for local `docker run`.
ENV PORT=8080
EXPOSE 8080

# config.py fail-closes if DATABASE_URL / LANDOS_SECRET are unset — set them as host secrets.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
