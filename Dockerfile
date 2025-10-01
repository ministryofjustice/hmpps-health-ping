FROM ghcr.io/astral-sh/uv:python3.13-alpine
WORKDIR /app

RUN addgroup -g 2000 appgroup && \
    adduser -u 2000 -G appgroup -h /home/appuser -D appuser

# initialise uv
COPY pyproject.toml .
RUN uv sync

# copy the dependencies from builder stage
COPY --chown=appuser:appgroup /home/appuser/.local /home/appuser/.local
COPY ./health_ping.py .

# update PATH environment variable
ENV PATH=/home/appuser/.local:$PATH

USER 2000

CMD [ "uv", "run", "python", "-u", "health_ping.py" ]
