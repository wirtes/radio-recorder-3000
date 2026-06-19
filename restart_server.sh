#!/bin/sh

set -eu

# Restart Radio Recorder 3000 from the directory containing this script.
cd "$(dirname "$0")"

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    echo "Docker Compose is not installed." >&2
    echo "Install the Docker Compose plugin, then run this script again." >&2
    exit 1
fi

echo "Rebuilding and restarting Radio Recorder 3000..."
$COMPOSE up --build --detach

echo
$COMPOSE ps
echo
echo "Radio Recorder 3000 is available at http://localhost:8585"
