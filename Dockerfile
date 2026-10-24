FROM python:3.13-alpine

# tzdata is needed for the TZ environment variable to take effect
RUN apk add --no-cache tzdata

WORKDIR /app
COPY app/ /app/

ENV PORT=7329 \
    TZ=UTC \
    PYTHONUNBUFFERED=1

EXPOSE 7329
USER 65534:65534

CMD ["python", "/app/server.py"]
