FROM python:3.12-slim

# fonts-liberation: a serif for the placard (frame_push falls back to it on Linux).
# iputils-ping + net-tools: TV discovery pings the subnet and reads the ARP table for the MAC;
#   the slim base image has neither, so discovery would fail outright.
# cron: the panel writes a crontab for the schedule, so the daemon has to exist to run it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-liberation iputils-ping net-tools cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY frame_push.py app.py ./

EXPOSE 8080
# The control panel, with cron running beside it so a saved schedule actually fires.
CMD ["sh", "-c", "cron && exec python app.py --port 8080"]
