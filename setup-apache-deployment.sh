#!/bin/bash
#
# SWF Monitor Apache Deployment Setup Script
# 
# This script sets up the Apache deployment infrastructure for swf-monitor
# Run with: sudo ./setup-apache-deployment.sh
#
# Creates:
# - /opt/swf-monitor/ deployment structure
# - Apache virtual host configuration
# - Deployment automation script
# - Production environment configuration

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

echo "Installing Apache virtual host configuration..."
cp "$SCRIPT_DIR/apache-swf-monitor.conf" "$APACHE_CONF_DIR/swf-monitor.conf"


echo "Creating production environment configuration..."
if [ ! -f "$DEPLOY_ROOT/config/env/production.env" ]; then
    cp "$SCRIPT_DIR/production.env" "$DEPLOY_ROOT/config/env/production.env"
    chmod 600 "$DEPLOY_ROOT/config/env/production.env"
    chown "$CURRENT_USER:eic" "$DEPLOY_ROOT/config/env/production.env"
    echo "Created production.env template - please review and update values"
else
    echo "Production environment file already exists, skipping..."
fi

echo "Installing required Apache modules..."
# Check if mod_wsgi is available and install if needed
if ! httpd -M 2>/dev/null | grep -q wsgi_module; then
    echo "Installing mod_wsgi..."
    yum install -y python3-mod_wsgi || {
        echo "Failed to install mod_wsgi. You may need to install it manually:"
        echo "  yum install python3-mod_wsgi"
        echo "Or compile from source if not available in repositories"
    }
fi

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
echo "Apache configuration: $APACHE_CONF_DIR/swf-monitor.conf"
echo "Deployment script: $DEPLOY_ROOT/bin/deploy-swf-monitor.sh"
echo ""
echo "Next steps:"
echo "1. Deploy your first release:"
echo "   sudo /opt/swf-monitor/bin/deploy-swf-monitor.sh branch infra/baseline-v18"
echo ""
echo "2. Test the deployment:"
echo "   curl https://localhost/"
echo ""
echo "Your development environment remains unchanged at:"
echo "   /eic/u/wenauseic/github/swf-monitor/"