# Backend image for Incident Sentinel.
#
# Build context is ./backend, so the repo-root .env is deliberately outside it:
# a container takes its settings from the environment, never from a baked-in
# secrets file.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first so the dependency layer survives application edits.
# psycopg is installed as [binary], so no libpq build chain is needed here.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Render (and most PaaS) inject the port to bind. Default to 8000 for
# `docker run -p 8000:8000` locally.
ENV PORT=8000
EXPOSE 8000

# Run as a non-root user: nothing here needs write access to the image.
RUN useradd --create-home --uid 1000 sentinel && chown -R sentinel:sentinel /app
USER sentinel

# JSON form keeps the shell out of the signal path, and `exec` replaces it with
# uvicorn as PID 1 -- so a platform SIGTERM on redeploy reaches the server and
# it shuts down gracefully instead of being killed. The `sh -c` wrapper is what
# still lets $PORT expand at runtime.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
