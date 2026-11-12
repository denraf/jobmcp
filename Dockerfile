# jobmcp application image: web (site + API + MCP), and the scheduler, from the same image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The application runs as this unprivileged user. (The container itself starts as root only so the entrypoint can
# hand over to it immediately; see docker/entrypoint.sh for why.)
RUN useradd --system --create-home --uid 10001 jobmcp

WORKDIR /app
COPY pyproject.toml ./
COPY jobmcp ./jobmcp
RUN pip install .

COPY docker/entrypoint.sh /usr/local/bin/jobmcp-entrypoint
RUN chmod +x /usr/local/bin/jobmcp-entrypoint

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# The entrypoint waits for MySQL (and prepares the schema), then runs the command.
ENTRYPOINT ["jobmcp-entrypoint"]
CMD ["jobmcp", "serve", "--host", "0.0.0.0", "--port", "8000"]
