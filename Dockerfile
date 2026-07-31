FROM ghcr.io/ministryofjustice/hmpps-python:python3.13-alpine AS base

USER root
RUN apk add --no-cache git
USER 2000

# initialise uv
COPY pyproject.toml .
RUN uv sync

# copy the software
COPY ./*.py .

CMD [ "uv", "run", "python", "-u", "main.py" ]
