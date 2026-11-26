FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1

CMD ["/app/docker-entrypoint.sh"]
