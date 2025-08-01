#!/bin/bash
# Script to start Django with both HTTP and HTTPS support
# HTTP on port 8002 for REST logging (no auth)
# HTTPS on port 8443 for authenticated API calls

echo "Starting Django with dual HTTP/HTTPS support..."

# Navigate to swf-monitor source directory
cd /eic/u/wenauseic/github/swf-monitor/src

# Activate virtual environment
source /eic/u/wenauseic/github/swf-testbed/.venv/bin/activate

# Kill existing Django servers if running
echo "Stopping existing Django servers..."
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

# Start HTTP server on port 8002 (for REST logging)
echo "Starting HTTP server on port 8002 for REST logging..."
python manage.py runserver 0.0.0.0:8002 &
HTTP_PID=$!

# Wait a moment for HTTP server to start
sleep 2

# Start HTTPS server on port 8443 (for authenticated API calls)
echo "Starting HTTPS server on port 8443 for authenticated APIs..."
if python -c "import django_extensions" 2>/dev/null; then
    python manage.py runserver_plus --cert-file "$SSL_CERT" --key-file "$SSL_KEY" 0.0.0.0:8443 &
    HTTPS_PID=$!
else
    # Fallback: Use Daphne with HTTPS
    daphne -p 8443 -b 0.0.0.0 --cert "$SSL_CERT" --key "$SSL_KEY" swf_monitor_project.asgi:application &
    HTTPS_PID=$!
fi

echo "Django servers started:"
echo "  HTTP (REST logging):     http://localhost:8002"
echo "  HTTPS (authenticated):   https://localhost:8443"
echo ""
echo "Process IDs: HTTP=$HTTP_PID, HTTPS=$HTTPS_PID"
echo "Press Ctrl+C to stop both servers"

# Wait for interrupt and clean up
trap 'echo "Stopping servers..."; kill $HTTP_PID $HTTPS_PID 2>/dev/null; exit' INT

# Keep script running
wait