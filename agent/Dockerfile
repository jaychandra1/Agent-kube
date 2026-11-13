FROM python:3.12-slim
# No .pyc files and unbuffered logs: the pod runs with a read-only root filesystem
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd -u 10001 agent
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=agent:agent . .
USER 10001
# 8000: API + Teams webhook (HTTP).  8443: activity webhook (HTTPS, only with a mounted certificate)
EXPOSE 8000 8443
CMD ["python", "main.py"]
