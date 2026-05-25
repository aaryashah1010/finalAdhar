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

For shared/network folders, use the staged launcher instead:

```text
run_docker_staged.bat
```

It copies all PDFs from one selected shared parent folder, including all
subfolders, into a local working folder before Docker starts. The detector
then only reads and moves local files, avoiding network-drive permission
problems inside Docker. After the app stops, the launcher can copy deleted
Aadhaar files back to a shared output folder and remove only the matching
original files from the shared input folder.

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

## Staged Network Folder Mode

Use this when the PDFs are on a mapped drive such as `T:` or a UNC path such
as `\\SERVER\Share\ParentFolder`.

Double-click:

```text
run_docker_staged.bat
```

The script will:

1. Ask for the shared parent folder.
2. Clear `client-data\staged-run\input`.
3. Copy all PDFs recursively from the shared folder into local input.
4. Start Docker using:

```text
client-data\staged-run\input
client-data\staged-run\deleted
```

Deleted Aadhaar files will be moved into:

```text
client-data\staged-run\deleted
```

After the Docker app stops, the script asks whether to sync deleted files
back to the shared drive. If you choose yes, it will:

1. Ask for a shared output folder.
2. Copy the deleted Aadhaar files there, preserving subfolders.
3. Verify the copied file hash.
4. Remove only the matching original file from the shared input folder.

The shared input folder is never cleared. Non-Aadhaar files stay in place.
Sync reports are written to:

```text
client-data\staged-run\reports
```

## What To Send To Client

Send the project folder with these files:

```text
Dockerfile
docker-compose.yml
docker-entrypoint.sh
run_docker.bat
run_docker_staged.bat
tools/sync_deleted_to_shared.ps1
DOCKER_RUN.md
requirements.txt
app.py
src/
.dockerignore
```

Do not send only the EXE. The Docker version does not use the EXE.
