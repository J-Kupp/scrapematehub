#!/usr/bin/env bash
set -euo pipefail

DATE_STAMP="$(date +%Y%m%d-%H%M%S)"
SOURCE_ROOT="/var/lib/yourbarmate-suppliers"
ARCHIVE_DIR="/var/backups/yourbarmate-suppliers"

mkdir -p "$ARCHIVE_DIR"
tar -czf "$ARCHIVE_DIR/yourbarmate-suppliers-$DATE_STAMP.tar.gz" \
  "$SOURCE_ROOT/control_panel" \
  "$SOURCE_ROOT/state" \
  "$SOURCE_ROOT/output"

echo "Backup written to $ARCHIVE_DIR/yourbarmate-suppliers-$DATE_STAMP.tar.gz"
