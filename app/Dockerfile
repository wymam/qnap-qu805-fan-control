FROM python:3.11-slim

# 容器默认跑 UTC，日志时间会差 8 小时；python:3.11-slim 自带 zoneinfo，设 TZ 即可
ENV TZ=Asia/Shanghai

WORKDIR /app
COPY fan_webui.py /app/fan_webui.py

EXPOSE 8080
VOLUME ["/data"]

ENV PYTHONUNBUFFERED=1
CMD ["python3", "-u", "/app/fan_webui.py"]
