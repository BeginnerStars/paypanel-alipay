FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY . .
RUN mkdir -p /app/data

EXPOSE 8000
CMD ["python", "-m", "app.main"]
