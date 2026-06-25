FROM python:3.11-slim

WORKDIR /app

# Кэшируем установку Python-пакетов: сначала копируем только requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Теперь копируем весь код (изменения в коде не затронут слой с pip)
COPY . .

EXPOSE 3000

CMD ["python", "main.py"]