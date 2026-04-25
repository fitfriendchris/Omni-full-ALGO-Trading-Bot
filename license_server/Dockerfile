FROM python:3.11-slim
WORKDIR /app
COPY license_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY license_server/app.py .
EXPOSE 5000
# Shell form so $PORT expands correctly at runtime
CMD gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 30 app:app
