FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir requests
# tzdata + TZ：容器默认是 UTC，不设时区的话每日掉落日期和消息时间都会差 8 小时
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY core.py kg_app.py ./
COPY static ./static

ENV KG_NO_BROWSER=1
ENV KG_DATA_DIR=/data
ENV GUEST_PORT=8766
ENV TZ=Asia/Shanghai
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo "Asia/Shanghai" > /etc/timezone
RUN mkdir -p /data
WORKDIR /data
VOLUME /data
EXPOSE 8765 8766

CMD ["python", "/app/kg_app.py", "--host", "0.0.0.0", "--port", "8765"]
