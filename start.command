#!/bin/bash
# Startet das Crypto-Sentiment-Dashboard und öffnet es im Browser.
# Beenden mit Strg+C oder durch Schließen des Terminal-Fensters.

cd "$(dirname "$0")" || exit 1
exec python3 server.py "$@"
