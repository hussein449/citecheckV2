# Temporary demo image — Hugging Face Spaces (Docker SDK).
#
# To undo the whole deployment later, delete this file and .dockerignore, drop
# the front-matter block at the top of README.md, and drop the
# CITECHECK_NO_SANDBOX block in citecheck/shots.py. Nothing else was touched.

# Pinned to bookworm deliberately. Playwright supports a fixed list of distros
# (Debian 12/13, Ubuntu 22.04/24.04/26.04); the unsuffixed python:3.12-slim tag
# follows Debian's latest, so an upstream bump would quietly drop the base off
# that list and fail the build at `playwright install` — several minutes in, on
# the very last layer.
FROM python:3.12-slim-bookworm

# Browsers live outside any home directory, so root can install them and the
# unprivileged runtime user can still launch them.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./

# gunicorn is deploy-only, so it is installed here rather than added to
# requirements.txt. Installing Chromium from the same environment that holds the
# playwright package keeps the two on versions that match.
RUN pip install --no-cache-dir -r requirements.txt gunicorn \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright

# Spaces runs the container as uid 1000. Copying straight to that owner beats a
# recursive chown afterwards, which rewrites the metadata of every file and so
# duplicates the whole tree into a fresh layer.
RUN useradd -m -u 1000 user
COPY --chown=user:user . .

# The app writes run artefacts and uploads alongside its own source. Only these
# three need to change hands, so the chown stays shallow.
RUN mkdir -p runs uploads && chown user:user /app runs uploads
USER user

ENV PORT=7860 \
    CITECHECK_NO_SANDBOX=1
EXPOSE 7860

# One worker is not a tuning choice. _RUNS in app.py is per-process state, so a
# second worker would strand an upload on one process and its progress stream on
# another, and the client would watch a run that never moves. --timeout 0 stops
# gunicorn reaping the SSE connections, which stay open for the whole run.
CMD gunicorn --workers 1 --threads 16 --timeout 0 --bind 0.0.0.0:$PORT app:app
