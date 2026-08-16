#!/usr/bin/env bash
# Consistent backup of the mirror: DB snapshot (safe under WAL) + document tree.
# Usage: scripts/backup.sh /path/to/backup/dir
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:?usage: backup.sh <dest-dir>}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DEST/tenders-$STAMP"
mkdir -p "$OUT"

# Consistent SQLite snapshot (works while the scraper writes, under WAL).
sqlite3 "$ROOT/data/tenders.db" ".backup '$OUT/tenders.db'"

# Documents + raw HTML provenance.
rsync -a "$ROOT/data/docs/"  "$OUT/docs/"
rsync -a "$ROOT/data/html/" "$OUT/html/"

echo "backup written to $OUT"
