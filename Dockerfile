# Apple Home bridge for the kitchen lights. Build and run with docker compose
# (see compose.yaml and the README section "Apple Home bridge").
FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
# amd64 / arm64: every dependency has a ready-made wheel. 32-bit ARM (armv7l,
# e.g. Odroid XU4 / HC1): cffi has none and must be compiled, so only then
# install a compiler, build, and remove the compiler again.
RUN pip install --no-cache-dir -r requirements.txt \
 || (apt-get update \
     && apt-get install -y --no-install-recommends build-essential libffi-dev \
     && pip install --no-cache-dir -r requirements.txt \
     && apt-get purge -y build-essential libffi-dev && apt-get autoremove -y \
     && rm -rf /var/lib/apt/lists/*)
COPY kitchenlights/ kitchenlights/

ENV PYTHONUNBUFFERED=1
# /data holds buttons.json (read-only) and homekit-state/ (pairing data), both
# mounted from the host. The Pico is passed in as /dev/pico.
ENTRYPOINT ["python", "-m", "kitchenlights.homekit", "--buttons", "/data/buttons.json", "--state-dir", "/data/homekit-state", "--port", "/dev/pico"]
