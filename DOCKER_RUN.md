# Docker Run Guide

This package runs the desktop app inside Docker and shows it in the browser with noVNC.

## Client PC Requirements

- Windows 10/11
- Docker Desktop installed and running
- Internet access on the first run so Docker can build the image
- 8 GB RAM minimum, 16 GB recommended

## Build and Start

Double-click:

```text
run_docker.bat
```

Or run manually:

```powershell
docker compose up --build
```

Open:

```text
http://localhost:6080/vnc.html?autoconnect=true&resize=scale
```

## Folders

Put PDFs to scan here:

```text
client-data/input
```

Deleted/moved files will appear here:

```text
client-data/output
```

Docker maps those folders into the container as `/data/input` and `/data/output`.

## What To Send To Client

Send the project folder with these files:

```text
Dockerfile
docker-compose.yml
docker-entrypoint.sh
run_docker.bat
DOCKER_RUN.md
requirements.txt
app.py
src/
.dockerignore
```

Do not send only the EXE. The Docker version does not use the EXE.
