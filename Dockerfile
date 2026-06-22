FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir requests paho-mqtt
COPY proxmox2mqtt.py .
CMD ["python", "-u", "proxmox2mqtt.py"]
