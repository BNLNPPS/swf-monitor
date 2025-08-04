#!/bin/bash
# Script to start Django with both HTTP and HTTPS support
# HTTP on port 8002 for REST logging (no auth)
# HTTPS on port 8443 for authenticated API calls

echo "Starting Django with dual HTTP/HTTPS support..."

# Source ~/.env if it exists
if [[ -f "$HOME/.env" ]]; then
    source "$HOME/.env"
fi

# Navigate to swf-monitor source directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWF_MONITOR_DIR="${SWF_MONITOR_DIR:-$SCRIPT_DIR}"
cd "$SWF_MONITOR_DIR/src"

# Activate virtual environment
SWF_TESTBED_DIR="${SWF_TESTBED_DIR:-$SCRIPT_DIR/../swf-testbed}"
if [[ -f "$SWF_TESTBED_DIR/.venv/bin/activate" ]]; then
    source "$SWF_TESTBED_DIR/.venv/bin/activate"
else
    echo "Warning: Virtual environment not found at $SWF_TESTBED_DIR/.venv"
    echo "Continuing without virtual environment activation..."
fi

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
# Use Daphne with proper SSL endpoint syntax
daphne -e ssl:8443:privateKey="$SSL_KEY":certKey="$SSL_CERT":interface=0.0.0.0 swf_monitor_project.asgi:application &
HTTPS_PID=$!

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