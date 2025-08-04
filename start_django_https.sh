#!/bin/bash
# Script to start Django with HTTPS support using self-signed certificate

echo "Starting Django with HTTPS support..."

# Navigate to swf-monitor source directory
cd /eic/u/wenauseic/github/swf-monitor/src

# Activate virtual environment
source /eic/u/wenauseic/github/swf-testbed/.venv/bin/activate

# Kill existing Django server if running
echo "Stopping existing Django server..."
pkill -f "manage.py runserver"
pkill -f "daphne"
sleep 2

# Check if SSL certificate exists, create self-signed if not
SSL_CERT="../ssl_cert.pem"
SSL_KEY="../ssl_key.pem"

if [[ ! -f "$SSL_CERT" || ! -f "$SSL_KEY" ]]; then
    echo "Creating self-signed SSL certificate for development..."
    openssl req -x509 -newkey rsa:4096 -keyout "$SSL_KEY" -out "$SSL_CERT" -sha256 -days 365 -nodes \
        -subj "/C=US/ST=NY/L=Upton/O=BNL/OU=NPPS/CN=localhost"
    echo "SSL certificate created: $SSL_CERT"
    echo "SSL private key created: $SSL_KEY"
fi

# Start Django with HTTPS using runserver_plus (django-extensions) if available
if python -c "import django_extensions" 2>/dev/null; then
    echo "Starting Django with HTTPS using runserver_plus on port 8002..."
    python manage.py runserver_plus --cert-file "$SSL_CERT" --key-file "$SSL_KEY" 0.0.0.0:8002
else
    # Fallback: Use Daphne with HTTPS
    echo "Starting Daphne with HTTPS on port 8002..."
    daphne -p 8002 -b 0.0.0.0 \
        --cert "$SSL_CERT" \
        --key "$SSL_KEY" \
        swf_monitor_project.asgi:application
fi