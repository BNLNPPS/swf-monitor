#!/bin/bash
# Require bash (fail fast if invoked under another shell)
if [ -z "${BASH_VERSION:-}" ]; then
    echo "This script must be run with bash. Try: bash $0 \"$@\"" >&2
    exit 1
fi
# Script to start Django with both HTTP and HTTPS support
# HTTP on port 8002 for REST logging (no auth)
# HTTPS on port 8443 for authenticated API calls

echo "Starting Django with dual HTTP/HTTPS support..."

# Robustly kill any process listening on a TCP port (TERM then KILL)
kill_port() {
    local port="$1"
    echo "Ensuring no process is listening on port ${port}..."

    # Collect PIDs via lsof if available
    local pids=""
    if command -v lsof >/dev/null 2>&1; then
        pids=$(lsof -ti tcp:"${port}" 2>/dev/null || true)
    fi

    # If still empty, try ss to parse PIDs
    if [ -z "${pids}" ] && command -v ss >/dev/null 2>&1; then
        # Example line contains users:("python",pid=1234,fd=5)
        pids=$(ss -ltnp "( sport = :${port} )" 2>/dev/null | awk -F',' '/pid=/ { for (i=1;i<=NF;i++) if ($i ~ /pid=/) { gsub(/.*pid=/, "", $i); gsub(/[^0-9].*/, "", $i); if ($i != "") print $i } }' | sort -u)
    fi

    # If still nothing, try fuser to kill directly
    if [ -z "${pids}" ] && command -v fuser >/dev/null 2>&1; then
        echo "Using fuser to kill listeners on ${port}/tcp (if any)"
        fuser -k "${port}/tcp" 2>/dev/null || true
        sleep 1
        # Re-check after fuser
        if command -v lsof >/dev/null 2>&1; then
            pids=$(lsof -ti tcp:"${port}" 2>/dev/null || true)
        fi
    fi

    if [ -n "${pids}" ]; then
        echo "Found PIDs on port ${port}: ${pids} — sending SIGTERM"
        kill -TERM ${pids} 2>/dev/null || true
        sleep 1
        # If still present, escalate to SIGKILL
        local remaining="${pids}"
        if command -v lsof >/dev/null 2>&1; then
            remaining=$(lsof -ti tcp:"${port}" 2>/dev/null || true)
        elif command -v ss >/dev/null 2>&1; then
            remaining=$(ss -ltnp "( sport = :${port} )" 2>/dev/null | awk -F',' '/pid=/ { for (i=1;i<=NF;i++) if ($i ~ /pid=/) { gsub(/.*pid=/, "", $i); gsub(/[^0-9].*/, "", $i); if ($i != "") print $i } }' | sort -u)
        fi
        if [ -n "${remaining}" ]; then
            echo "Processes still listening on ${port}: ${remaining} — sending SIGKILL"
            kill -KILL ${remaining} 2>/dev/null || true
            sleep 1
        fi
    fi

    # Final check
    local check=""
    if command -v lsof >/dev/null 2>&1; then
        check=$(lsof -ti tcp:"${port}" 2>/dev/null || true)
    elif command -v ss >/dev/null 2>&1; then
        check=$(ss -ltnp "( sport = :${port} )" 2>/dev/null | grep -v "State" || true)
    fi
    if [ -z "${check}" ]; then
        echo "Port ${port} is free."
    else
        echo "Warning: Port ${port} still appears in use. You may need elevated permissions to terminate owning processes."
    fi
}

# Source ~/.env if it exists
if [[ -f "$HOME/.env" ]]; then
    source "$HOME/.env"
fi

# Navigate to swf-monitor source directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWF_MONITOR_DIR="${SWF_MONITOR_DIR:-$SCRIPT_DIR}"
cd "$SWF_MONITOR_DIR/src"

# Set up virtual environment paths
SWF_TESTBED_DIR="${SWF_TESTBED_DIR:-$SCRIPT_DIR/../swf-testbed}"
if [[ -f "$SWF_TESTBED_DIR/.venv/bin/activate" ]]; then
    source "$SWF_TESTBED_DIR/.venv/bin/activate"
    PYTHON_CMD="$SWF_TESTBED_DIR/.venv/bin/python"
    DAPHNE_CMD="$SWF_TESTBED_DIR/.venv/bin/daphne"
else
    echo "Warning: Virtual environment not found at $SWF_TESTBED_DIR/.venv"
    echo "Continuing with system python..."
    PYTHON_CMD="python"
    DAPHNE_CMD="daphne"
fi

# Kill existing Django servers if running
echo "Stopping existing Django servers..."
# Limit to current user and silence errors to avoid noisy 'Operation not permitted'
if command -v pkill >/dev/null 2>&1; then
    pkill -u "$USER" -f "manage.py runserver" >/dev/null 2>&1 || true
    pkill -u "$USER" -f "daphne" >/dev/null 2>&1 || true
fi
sleep 2

# Ensure 8443 is completely free before starting Daphne
kill_port 8443

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
$PYTHON_CMD manage.py runserver 0.0.0.0:8002 &
HTTP_PID=$!

# Wait a moment for HTTP server to start
sleep 2

# Start HTTPS server on port 8443 (for authenticated API calls)
echo "Starting HTTPS server on port 8443 for authenticated APIs..."
# Use Daphne with proper SSL endpoint syntax
$DAPHNE_CMD -e ssl:8443:privateKey="$SSL_KEY":certKey="$SSL_CERT":interface=0.0.0.0 swf_monitor_project.asgi:application &
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