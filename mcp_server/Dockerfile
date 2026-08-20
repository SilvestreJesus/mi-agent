FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir "fastmcp>=2.0" python-dotenv

COPY . .

EXPOSE 8000
CMD ["python", "server.py"]
