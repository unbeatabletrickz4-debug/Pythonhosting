FROM python:3.10-slim

WORKDIR /app

# Install system utilities for process management
RUN apt-get update && apt-get install -y procps build-essential

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "bot.py"]
