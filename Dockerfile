FROM ghcr.io/ministryofjustice/hmpps-python:python3.13-alpine AS base

# initialise uv
COPY pyproject.toml .
RUN uv sync

# copy the software
COPY ./*.py .

CMD [ "uv", "run", "python", "-u", "main.py" ]
