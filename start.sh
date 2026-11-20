#!/bin/sh
# Migrate, seed defaults, then run the voice worker next to the web server in the same container.
set -e
python manage.py migrate --noinput
python manage.py seed_defaults
python manage.py run_worker &
exec gunicorn serviq.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_WORKERS:-3} --timeout 120 --access-logfile -
