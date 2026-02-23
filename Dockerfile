FROM dhi.io/python:3.11-debian13

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

RUN set -eux; \
    groupadd -r app; \
    useradd -r -g app -d /app -s /usr/sbin/nologin app; \
    mkdir -p /app; \
    chown -R app:app /app

COPY --chown=app:app requirements.txt /app/requirements.txt

RUN set -eux; \
    pip install --no-cache-dir --upgrade pip; \
    pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=app:app app.py /app/app.py

USER app:app

ENTRYPOINT ["python3", "/app/app.py"]
CMD []

