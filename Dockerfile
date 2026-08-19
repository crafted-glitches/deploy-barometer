FROM python:3.14-slim

# uvicorn binds inside the container; compose maps it out to the host.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    BAROMETER_HOST=0.0.0.0 \
    BAROMETER_PORT=2323

WORKDIR /srv

# Dependencies first, so edits to the source do not invalidate this layer.
COPY pyproject.toml README.md LICENSE LICENSING.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Drop privileges: nothing here needs root.
RUN useradd --create-home --uid 10001 barometer
USER barometer

EXPOSE 2323

# The bar is reached over the network, so a failure here means the app itself
# is wedged rather than the device being asleep.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:2323/health', timeout=4).status < 500 else 1)" \
    || exit 1

CMD ["deploy-barometer"]
