#!/bin/bash
set -e

echo "==> Waiting for PostgreSQL …"
until python -c "
import socket, sys, os
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.connect((os.environ.get('DB_HOST','db'), int(os.environ.get('DB_PORT','5432'))))
    s.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; do
    sleep 1
done
echo "==> PostgreSQL is up."

echo "==> Running database migrations …"
python manage.py migrate --noinput

echo "==> Creating Django superuser (if configured) …"
if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
    python manage.py createsuperuser --noinput 2>/dev/null || true
else
    echo "    Skipped — DJANGO_SUPERUSER_USERNAME or DJANGO_SUPERUSER_PASSWORD not set."
fi

echo "==> Collecting static files …"
python manage.py collectstatic --noinput 2>/dev/null || true

echo "==> Starting server …"
exec "$@"
