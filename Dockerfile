ARG PYTHON_IMAGE=python:3.15-slim-trixie
FROM ${PYTHON_IMAGE}
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/holloway25/Fan-Controller" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.revision="${REVISION}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FAN_CONTROLLER_DATA_DIR=/data FAN_CONTROLLER_PERSIST_STATE=1 \
    FAN_CONTROLLER_BIND=0.0.0.0 FAN_CONTROLLER_TRUST_PROXY=0 TZ=UTC
WORKDIR /app
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 fan && useradd --uid 10001 --gid 10001 --no-create-home fan \
    && install -d -o fan -g fan -m 0700 /data
COPY requirements.txt /tmp/requirements-original.txt
COPY container/requirements.lock /tmp/requirements.txt
# Packaging tools are build-time only. Remove their private dependency copies
# and ensurepip's bundled installer wheel from the final runtime filesystem.
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && python -m pip uninstall --yes setuptools wheel \
    && python -m pip uninstall --yes pip \
    && python -c "import ensurepip,pathlib,shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent)"
COPY controller.py config.example.json LICENSE /app/
COPY templates /app/templates/
COPY static /app/static/
COPY container/start.py /app/start.py
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8096
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8096/api/state', timeout=4).close()"]
ENTRYPOINT ["python", "/app/start.py"]
