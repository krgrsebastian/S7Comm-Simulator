FROM python:3.12-slim

# python-snap7 1.3 bundles the native libsnap7 in its wheel for linux x86_64
# and aarch64, so no extra system packages are required. The pin matters: 3.0+
# swaps in a pure-Python server that gos7 cannot handshake with (see
# requirements.txt).
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY simulator.py .
COPY profiles/ ./profiles/

# S7comm / ISO-on-TCP
EXPOSE 102

CMD ["python", "simulator.py"]
