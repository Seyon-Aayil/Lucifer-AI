FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx
COPY server.py .
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
