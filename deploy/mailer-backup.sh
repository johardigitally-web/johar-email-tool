#!/usr/bin/env bash
# Daily snapshot of the mailer database.
#
# VACUUM INTO rather than a file copy: the database runs in WAL mode, so the
# .sqlite3 file on its own is not a consistent database while the service is
# running. VACUUM INTO asks SQLite for a proper copy.
set -euo pipefail

SRC=/var/lib/mailer/mailer.sqlite3
DEST_DIR=/var/backups/mailer
STAMP=$(date +%F)
OUT="$DEST_DIR/mailer-$STAMP.sqlite3"

mkdir -p "$DEST_DIR"
# Contains names and email addresses. Root only.
chmod 700 "$DEST_DIR"

rm -f "$OUT"
/opt/mailer/.venv/bin/python - "$SRC" "$OUT" <<'PY'
import sqlite3, sys
src, out = sys.argv[1], sys.argv[2]
c = sqlite3.connect(src)
c.execute("VACUUM INTO ?", (out,))
c.close()
PY

# Prove it is a database before it is allowed to replace yesterday's good copy.
n=$(/opt/mailer/.venv/bin/python -c "
import sqlite3,sys
c=sqlite3.connect(sys.argv[1])
print(c.execute('select count(*) from subscriber').fetchone()[0])
" "$OUT")
if [ -z "$n" ]; then
  echo "backup did not verify, keeping the previous one" >&2
  rm -f "$OUT"
  exit 1
fi
gzip -f "$OUT"
# The directory is already root-only, but the file should not rely on that
# alone: it is a list of real people and it gets copied elsewhere.
chmod 600 "$OUT.gz"
echo "backed up $n subscribers to $OUT.gz"

# 14 dailies, plus the first of each month kept for six months.
find "$DEST_DIR" -name 'mailer-*-*-*.sqlite3.gz' ! -name 'mailer-*-*-01.sqlite3.gz' \
     -mtime +14 -delete
find "$DEST_DIR" -name 'mailer-*-*-01.sqlite3.gz' -mtime +185 -delete
