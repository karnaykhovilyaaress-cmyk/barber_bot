FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# BotHost ожидает, что приложение слушает порт из переменной PORT
EXPOSE 3000

CMD ["python", "bot.py"]
