FROM python:3.12-slim
RUN pip install --no-cache-dir minidisc-python
COPY agent.py /app/agent.py
WORKDIR /app
CMD ["python", "agent.py"]
