FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir requests
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY core.py kg_app.py ./
COPY static ./static

ENV KG_NO_BROWSER=1
ENV KG_DATA_DIR=/data
ENV GUEST_PORT=8766
RUN mkdir -p /data
WORKDIR /data
VOLUME /data
EXPOSE 8765 8766

CMD ["python", "/app/kg_app.py", "--host", "0.0.0.0", "--port", "8765"]
