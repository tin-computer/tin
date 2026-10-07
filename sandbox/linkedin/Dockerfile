# Separate build context: repo root. Never rebuild an existing Tin workflow alias.
FROM python:3.12.8-slim
RUN pip install --no-cache-dir httpx==0.28.1 pydantic==2.11.10 \
    && mkdir -m 700 /run/tin-collection \
    && mkdir -p /opt/tin-collection/tin_lite \
    && touch /opt/tin-collection/tin_lite/__init__.py
COPY src/tin_lite/connection_collection.py src/tin_lite/linkedin_session.py /opt/tin-collection/tin_lite/
COPY sandbox/linkedin/runner.py /opt/tin-collection/runner.py
RUN chmod -R go-rwx /opt/tin-collection
