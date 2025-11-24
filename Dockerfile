FROM ghcr.io/ministryofjustice/hmpps-python:python3.13-alpine AS base
 
# add the necessary libraries
USER root
RUN apk add --no-cache gcc python3-dev musl-dev linux-headers git ca-certificates && update-ca-certificates
USER 2000

# initialise uv
COPY pyproject.toml .
RUN uv sync

# copy the software
COPY ./*.py .

CMD [ "uv", "run", "python", "-u", "main.py" ]
