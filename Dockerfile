FROM python:3.10-slim

# Install Google Chrome and dependencies required for headless scraping
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables for Chrome
ENV RENDER=true
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy and install Python requirements directly
COPY . /app
RUN pip install --no-cache-dir flask flask-sqlalchemy flask-cors selenium selenium-stealth beautifulsoup4

EXPOSE 10000

# Run the app using Gunicorn or direct python
CMD ["python", "app.py"]
