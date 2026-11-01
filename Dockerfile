FROM ghcr.io/osgeo/gdal:ubuntu-small-3.13.3

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt cut_cell.py ./
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

ENTRYPOINT ["/opt/venv/bin/python", "/app/cut_cell.py"]
