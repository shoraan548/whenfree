FROM python:3.13-alpine
RUN apk add --no-cache tzdata
WORKDIR /app
COPY server.py index.html ./
CMD ["python", "server.py"]
