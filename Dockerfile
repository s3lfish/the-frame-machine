FROM python:3.12-slim

# LiberationSerif gives the placard a serif font on Linux (frame_push falls back to it).
RUN apt-get update && apt-get install -y --no-install-recommends fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY frame_push.py app.py ./

EXPOSE 8080
# The control panel. Settings, preview and "change now" work out of the box.
CMD ["python", "app.py", "--port", "8080"]
