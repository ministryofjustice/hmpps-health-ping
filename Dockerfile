FROM ghcr.io/ministryofjustice/hmpps-python:python3.13-alpine AS base
 
# add the necessary libraries
RUN apk add --no-cache gcc python3-dev musl-dev linux-headers git ca-certificates && update-ca-certificates

# initialise uv
COPY pyproject.toml .
RUN uv sync

# copy the software
COPY ./*.py .

RUN chown -R 2000:2000 /app

USER 2000

CMD [ "uv", "run", "python", "-u", "main.py" ]
