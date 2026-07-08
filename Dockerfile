# Restoration-services intake agent.
#
# Build:
#   docker build -t restoration-intake .
#
# Selftest (the default command — offline, no API key needed):
#   docker run --rm restoration-intake
#
# Live evals (real API calls; the key stays outside the image):
#   docker run --rm --env-file .env restoration-intake python3 agent.py --evals
#
# One-off request:
#   docker run --rm --env-file .env restoration-intake python3 agent.py "basement flooded, carpet soaked"
#
# Dev servers (UI :3000, API :8000) — INTAKE_BIND_HOST=0.0.0.0 so the published
# ports are reachable from outside the container's network namespace:
#   docker run --rm --env-file .env -e INTAKE_BIND_HOST=0.0.0.0 \
#     -p 3000:3000 -p 8000:8000 restoration-intake python3 agent.py --serve
#
# After the web workstream merges, the orchestrator may install
# requirements.txt and point CMD at the web layer instead.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# agent.py's only dependency. Installed before COPY so code edits don't bust
# this layer. Floor matches the version the suite runs against locally.
RUN pip install --no-cache-dir "anthropic>=0.84,<1"

# .dockerignore keeps .env, git history, and handoff notes out of the context.
COPY . .

# Nothing here needs root.
RUN useradd --system --no-create-home intake
USER intake

CMD ["python3", "agent.py", "--selftest"]
