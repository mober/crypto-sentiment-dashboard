#!/usr/bin/env bash
# Startet das Crypto-Sentiment-Dashboard und öffnet es im Browser.
#   ./start.sh                  → http://localhost:8000
#   ./start.sh --port 8080      → anderer Port
#   ./start.sh --no-browser     → Browser nicht öffnen
# Beenden mit Strg+C.
set -euo pipefail
cd "$(dirname "$0")"

PORT=8000
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ ${args[i]} == --port ]] && PORT=${args[i+1]:-8000}
  [[ ${args[i]} == --port=* ]] && PORT=${args[i]#--port=}
done

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT ist schon belegt – läuft der Server bereits? → http://localhost:$PORT"
  echo "Laufenden Server beenden: kill \$(lsof -t -iTCP:$PORT -sTCP:LISTEN)"
  exit 1
fi

exec python3 server.py "$@"
