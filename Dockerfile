FROM python:3.12-slim

# python-snap7 3.0.0 bundles the native snap7 library in its wheel for
# linux x86_64 and aarch64, so no extra system packages are required.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY simulator.py .

# S7comm / ISO-on-TCP
EXPOSE 102

CMD ["python", "simulator.py"]
