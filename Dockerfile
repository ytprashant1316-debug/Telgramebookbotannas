FROM python:3.11-slim

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Copy requirements first for caching
COPY requirements.in .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.in

# Copy the rest of the application
COPY . .

# Start the bot via polling
CMD ["python", "bot.py"]
