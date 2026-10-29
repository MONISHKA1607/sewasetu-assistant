FROM python:3.11-slim
WORKDIR /app
COPY mcp_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# shared rules + config come from the portal's own code, so there is one source of truth
COPY app/service.py /app/service.py
COPY app/config /app/config
COPY mcp_service/*.py /app/
ENV PYTHONUNBUFFERED=1
CMD ["python", "citizen_server.py"]
