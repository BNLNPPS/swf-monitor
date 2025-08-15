#!/bin/bash
#
# SWF Monitor Apache Deployment Setup Script
# 
# USAGE: sudo ./setup-apache-deployment.sh
# 
# See docs/PRODUCTION_DEPLOYMENT.md for complete documentation

set -e  # Exit on any error

DEPLOY_ROOT="/opt/swf-monitor"
APACHE_CONF_DIR="/etc/httpd/conf.d"
REPO_URL="https://github.com/BNLNPPS/swf-monitor.git"
CURRENT_USER="wenauseic"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== SWF Monitor Apache Deployment Setup ==="
echo "This will create production deployment infrastructure"
echo "Press Enter to continue or Ctrl+C to cancel"
read

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (with sudo)" 
   exit 1
fi

# Verify required files exist
REQUIRED_FILES=("deploy-swf-monitor.sh" "apache-swf-monitor.conf" "production.env")
for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$SCRIPT_DIR/$file" ]; then
        echo "ERROR: Required file $file not found in $SCRIPT_DIR"
        exit 1
    fi
done

echo "Creating deployment directory structure..."
mkdir -p "$DEPLOY_ROOT"/{releases,shared/{logs,static,uploads},config/{apache,env},bin}

echo "Setting ownership and permissions..."
chown -R "$CURRENT_USER:eic" "$DEPLOY_ROOT"
chmod -R 755 "$DEPLOY_ROOT"

echo "Installing deployment automation script..."
cp "$SCRIPT_DIR/deploy-swf-monitor.sh" "$DEPLOY_ROOT/bin/deploy-swf-monitor.sh"
chmod +x "$DEPLOY_ROOT/bin/deploy-swf-monitor.sh"

# Apache configuration will be installed after mod_wsgi setup


echo "Creating production environment configuration..."
if [ ! -f "$DEPLOY_ROOT/config/env/production.env" ]; then
    cp "$SCRIPT_DIR/production.env" "$DEPLOY_ROOT/config/env/production.env"
    chmod 600 "$DEPLOY_ROOT/config/env/production.env"
    chown "$CURRENT_USER:eic" "$DEPLOY_ROOT/config/env/production.env"
    echo "Created production.env template - please review and update values"
else
    echo "Production environment file already exists, skipping..."
fi

echo "Installing required Apache modules and dependencies..."

# Install Apache development headers (required for mod_wsgi compilation)
echo "Installing Apache development headers..."
yum install -y httpd-devel || {
    echo "Failed to install httpd-devel. This is required for mod_wsgi compilation."
    exit 1
}

# Install Python 3.11 mod_wsgi in the project's virtual environment
echo "Installing Python 3.11 mod_wsgi in project virtual environment..."
VENV_PATH="/eic/u/wenauseic/github/swf-testbed/.venv"
if [ ! -d "$VENV_PATH" ]; then
    echo "ERROR: Project virtual environment not found at $VENV_PATH"
    echo "Please ensure swf-testbed is set up with: cd /eic/u/wenauseic/github/swf-testbed && source install.sh"
    exit 1
fi

# Activate venv and install mod_wsgi
cd /eic/u/wenauseic/github/swf-testbed
source .venv/bin/activate
source ~/.env || true  # Load environment but don't fail if missing

pip install mod_wsgi || {
    echo "Failed to install mod_wsgi in Python 3.11 virtual environment"
    exit 1
}

# Generate mod_wsgi configuration for Python 3.11
echo "Generating Python 3.11 mod_wsgi configuration..."
MODWSGI_CONFIG=$(mod_wsgi-express module-config)
echo "Generated mod_wsgi config:"
echo "$MODWSGI_CONFIG"

# Extract the module path and python home
MODWSGI_MODULE=$(echo "$MODWSGI_CONFIG" | grep "LoadModule" | cut -d'"' -f2)
MODWSGI_PYTHON_HOME=$(echo "$MODWSGI_CONFIG" | grep "WSGIPythonHome" | cut -d'"' -f2)

if [ -z "$MODWSGI_MODULE" ] || [ -z "$MODWSGI_PYTHON_HOME" ]; then
    echo "ERROR: Failed to extract mod_wsgi configuration"
    exit 1
fi

echo "mod_wsgi module: $MODWSGI_MODULE"
echo "Python home: $MODWSGI_PYTHON_HOME"

# Disable system mod_wsgi to avoid conflicts
echo "Disabling system mod_wsgi to avoid Python version conflicts..."
SYSTEM_MODWSGI="/etc/httpd/conf.modules.d/10-wsgi-python3.conf"
if [ -f "$SYSTEM_MODWSGI" ]; then
    mv "$SYSTEM_MODWSGI" "${SYSTEM_MODWSGI}.disabled"
    echo "Disabled system mod_wsgi at $SYSTEM_MODWSGI"
fi

# Create updated Apache configuration with Python 3.11 mod_wsgi
echo "Creating Apache configuration with Python 3.11 mod_wsgi..."
cat > "$SCRIPT_DIR/apache-swf-monitor-generated.conf" << EOF
#
# SWF Monitor Apache Configuration with Python 3.11 mod_wsgi
# Generated automatically by setup-apache-deployment.sh
# 
# This serves the Django application via Python 3.11 mod_wsgi
# Static files are served directly by Apache
#
# NOTE: This configuration works with the existing Apache SSL setup.
# SSL configuration is handled by the system's ssl.conf file.

# Load Python 3.11 mod_wsgi module (generated automatically)
LoadModule wsgi_module "$MODWSGI_MODULE"
WSGIPythonHome "$MODWSGI_PYTHON_HOME"

# WSGI Configuration for SWF Monitor
WSGIDaemonProcess swf-monitor \
    python-path=/opt/swf-monitor/current/src:/opt/swf-monitor/current/.venv/lib/python3.11/site-packages \
    python-home=/opt/swf-monitor/current/.venv \
    processes=2 \
    threads=15 \
    display-name=%{GROUP} \
    lang='en_US.UTF-8' \
    locale='en_US.UTF-8'

WSGIScriptAlias /swf-monitor /opt/swf-monitor/current/src/swf_monitor_project/wsgi.py process-group=swf-monitor

# Static files served directly by Apache
Alias /swf-monitor/static /opt/swf-monitor/shared/static
<Directory /opt/swf-monitor/shared/static>
    Require all granted
</Directory>

# WSGI script permissions
<Directory /opt/swf-monitor/current/src/swf_monitor_project>
    <Files wsgi.py>
        Require all granted
    </Files>
</Directory>

# Security headers for SWF Monitor
<Location /swf-monitor>
    Header always set X-Content-Type-Options nosniff
    Header always set X-Frame-Options DENY
    Header always set X-XSS-Protection "1; mode=block"
</Location>
EOF

echo "Generated Apache configuration at: $SCRIPT_DIR/apache-swf-monitor-generated.conf"

# Install the generated Apache configuration
echo "Installing Apache virtual host configuration with Python 3.11 mod_wsgi..."
cp "$SCRIPT_DIR/apache-swf-monitor-generated.conf" "$APACHE_CONF_DIR/swf-monitor.conf"

# Check if required modules are enabled (warn only, don't try to fix)
if ! httpd -M 2>/dev/null | grep -q rewrite_module; then
    echo "WARNING: mod_rewrite not found. HTTPS redirects may not work."
    echo "Please ensure mod_rewrite is enabled by your system administrator."
fi

if ! httpd -M 2>/dev/null | grep -q ssl_module; then
    echo "WARNING: mod_ssl not found. HTTPS will not work."
    echo "Please ensure SSL is properly configured by your system administrator."
fi

if ! httpd -M 2>/dev/null | grep -q headers_module; then
    echo "WARNING: mod_headers not found. Security headers will not work."
    echo "Please ensure mod_headers is enabled by your system administrator."
fi

echo "Testing Apache configuration..."
httpd -t || {
    echo "Apache configuration test failed. Please check the configuration."
    exit 1
}

echo "Restarting Apache..."
systemctl restart httpd
systemctl enable httpd

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "Deployment structure created at: $DEPLOY_ROOT"
echo "Apache configuration: $APACHE_CONF_DIR/swf-monitor.conf (with Python 3.11 mod_wsgi)"
echo "Deployment script: $DEPLOY_ROOT/bin/deploy-swf-monitor.sh"
echo "mod_wsgi module: $MODWSGI_MODULE"
echo "Python home: $MODWSGI_PYTHON_HOME"
echo ""
echo "Next steps:"
echo "1. Deploy your first release:"
echo "   sudo /opt/swf-monitor/bin/deploy-swf-monitor.sh branch infra/baseline-v18"
echo ""
echo "2. Test the deployment:"
echo "   curl http://localhost/swf-monitor/"
echo ""
echo "Your development environment remains unchanged at:"
echo "   /eic/u/wenauseic/github/swf-monitor/"