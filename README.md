# Fika Core

Backend server for Fika — a travel itinerary planner.

## Prerequisites

- Supabase database (set up via [fika-prep](https://github.com/kahgin/fika-prep))
- OSRM server for routing (see [OSRM Setup](#osrm-setup))

## Getting Started

> [!NOTE]
> Install [uv](https://docs.astral.sh/uv/getting-started/installation/) before proceeding.

### 1. Install dependencies

```bash
make
```

### 2. Set up environment

```bash
cp .env.example .env
```

### 3. Start the server

```bash
make dev
```

## OSRM Setup

This project requires an OSRM server for route calculations.

### Using Docker

```bash
# Download map data (example: Malaysia-Singapore)
wget https://download.geofabrik.de/asia/malaysia-singapore-brunei-latest.osm.pbf

# Pre-process the data
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend osrm-extract -p /opt/car.lua /data/malaysia-singapore-brunei-latest.osm.pbf
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend osrm-partition /data/malaysia-singapore-brunei-latest.osrm
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend osrm-customize /data/malaysia-singapore-brunei-latest.osrm

# Run the server
docker run -t -p 5000:5000 -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend osrm-routed --algorithm mld /data/malaysia-singapore-brunei-latest.osrm
```

Set `OSRM_URL=http://localhost:5000` in your `.env`.

> [!IMPORTANT]
> Pair this server with [Fika Web](https://github.com/kahgin/fika-front).
