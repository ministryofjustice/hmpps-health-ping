FROM ghcr.io/astral-sh/uv:python3.13-alpine
WORKDIR /app

RUN addgroup -g 2000 appgroup && \
    adduser -u 2000 -G appgroup -h /home/appuser -D appuser
 
# add the necessary libraries
RUN apk add --no-cache gcc python3-dev musl-dev linux-headers git ca-certificates && update-ca-certificates

# initialise uv
COPY pyproject.toml .
RUN uv sync

# copy the software
COPY ./*.py .

# update PATH environment variable
ENV PATH=/home/appuser/.local:$PATH

RUN chown -R 2000:2000 /app

USER 2000

CMD [ "uv", "run", "python", "-u", "main.py" ]
