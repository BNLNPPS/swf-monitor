#!/bin/bash
# Require bash (fail fast if invoked under another shell)
if [ -z "${BASH_VERSION:-}" ]; then
    echo "This script must be run with bash. Try: bash $0 \"$@\"" >&2
    exit 1
fi
#
# SWF Monitor Deployment Script
# Usage: deploy-swf-monitor.sh [tag|branch] <reference>
#
# This script is used to deploy the specified branch to the production apache
# system service on pandaserver02.sdcc.bnl.gov
#
# Examples:
#   deploy-swf-monitor.sh branch infra/baseline-v18
#   deploy-swf-monitor.sh branch main
#   deploy-swf-monitor.sh tag tagName        (tags not in use as of 9/2025)
#
# See docs/PRODUCTION_DEPLOYMENT.md for complete documentation

set -e

DEPLOY_ROOT="/opt/swf-monitor"
REPO_URL="https://github.com/BNLNPPS/swf-monitor.git"
CURRENT_USER="wenauseic"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (with sudo)" 
   exit 1
fi

if [ $# -ne 2 ]; then
    echo "Usage: $0 {tag|branch} <reference>"
    echo ""
    echo "Examples:"
    echo "  $0 tag infra/baseline-v17"
    echo "  $0 branch infra/baseline-v18"
    exit 1
fi

REF_TYPE="$1"
REF_VALUE="$2"

case "$REF_TYPE" in
    tag)
        GIT_REF="$REF_VALUE"
        DEPLOY_NAME="$REF_VALUE"
        ;;
    branch)
        GIT_REF="$REF_VALUE"
        DEPLOY_NAME="branch-$(echo $REF_VALUE | sed 's|/|-|g')"
        ;;
    *)
        echo "Invalid reference type: $REF_TYPE. Use 'tag' or 'branch'"
        exit 1
        ;;
esac

RELEASE_DIR="$DEPLOY_ROOT/releases/$DEPLOY_NAME"

log "Starting deployment of $REF_TYPE '$REF_VALUE' to '$DEPLOY_NAME'"

# Verify the branch/tag exists before proceeding
log "Verifying $REF_TYPE '$REF_VALUE' exists in repository..."
if ! git ls-remote --exit-code --heads --tags "$REPO_URL" "$GIT_REF" >/dev/null 2>&1; then
    echo "ERROR: $REF_TYPE '$REF_VALUE' does not exist in repository $REPO_URL"
    exit 1
fi

# Create release directory
if [ -d "$RELEASE_DIR" ]; then
    log "Release directory exists, removing..."
    rm -rf "$RELEASE_DIR"
fi

log "Creating release directory: $RELEASE_DIR"
mkdir -p "$RELEASE_DIR"

# Clone repository
log "Cloning repository..."
cd "$RELEASE_DIR"
git clone --single-branch --branch "$GIT_REF" "$REPO_URL" . || {
    echo "ERROR: Failed to clone $REF_TYPE '$REF_VALUE'"
    rm -rf "$RELEASE_DIR"
    exit 1
}

log "Checked out: $(git rev-parse --short HEAD) - $(git log -1 --pretty=format:'%s')"

# Copy development virtual environment
log "Copying development virtual environment..."
cp -r /eic/u/wenauseic/github/swf-testbed/.venv .venv
source .venv/bin/activate

# Verify production environment file exists
if [ ! -f "$DEPLOY_ROOT/config/env/production.env" ]; then
    echo "ERROR: Production environment file not found at $DEPLOY_ROOT/config/env/production.env"
    echo "Please create this file with appropriate production configuration before deploying."
    echo "See docs/PRODUCTION_DEPLOYMENT.md for configuration details."
    exit 1
fi

# Validate subpath configuration for Apache deployment
log "Validating subpath configuration..."
if grep -q "WSGIScriptAlias /swf-monitor" /etc/httpd/conf.d/swf-monitor.conf 2>/dev/null; then
    if ! grep -q "SWF_DEPLOYMENT_SUBPATH=/swf-monitor" "$DEPLOY_ROOT/config/env/production.env"; then
        echo "ERROR: Apache configured for /swf-monitor subpath but production.env missing subpath configuration"
        echo "Required variables in production.env:"
        echo "  SWF_DEPLOYMENT_SUBPATH=/swf-monitor"
        echo "  SWF_STATIC_URL_BASE=/swf-monitor/static/"
        echo "  SWF_LOGIN_REDIRECT=/swf-monitor/home/"
        echo "See docs/PRODUCTION_DEPLOYMENT.md for complete configuration details."
        exit 1
    fi
    log "✅ Subpath configuration validated"
else
    log "ℹ️ No subpath deployment detected in Apache config"
fi

# Link shared resources
# NOTE: .env is NOT deployed from git (it's in .gitignore for security).
# Production uses: $DEPLOY_ROOT/config/env/production.env
# To update production .env settings, edit that file directly.
log "Linking shared resources..."
ln -sf "$DEPLOY_ROOT/shared/logs" "$RELEASE_DIR/logs"
ln -sf "$DEPLOY_ROOT/config/env/production.env" "$RELEASE_DIR/.env"
log "  .env source: $DEPLOY_ROOT/config/env/production.env (edit this file for config changes)"

# Install WSGI module configuration if it exists in repository
if [ -f "$RELEASE_DIR/config/apache/20-swf-monitor-wsgi.conf" ]; then
    log "Installing WSGI module configuration..."
    cp "$RELEASE_DIR/config/apache/20-swf-monitor-wsgi.conf" /etc/httpd/conf.modules.d/20-swf-monitor-wsgi.conf
fi

# SSL certificate is already present from git clone if it exists in the repo
if [ -f "$RELEASE_DIR/full-chain.pem" ]; then
    log "SSL certificate found in deployment..."
fi

# Collect static files
log "Collecting static files..."
cd "$RELEASE_DIR/src"
export DJANGO_SETTINGS_MODULE=swf_monitor_project.settings
python manage.py collectstatic --noinput --clear --settings=swf_monitor_project.settings

# Copy static files to shared location
log "Copying static files to shared location..."
rsync -a --delete "$RELEASE_DIR/src/staticfiles/" "$DEPLOY_ROOT/shared/static/"

# Run database migrations
log "Running database migrations..."
python manage.py migrate --settings=swf_monitor_project.settings

# Set ownership
log "Setting ownership..."
chown -R "$CURRENT_USER:eic" "$DEPLOY_ROOT"

# Stop Apache
log "Stopping Apache..."
systemctl stop httpd

# Update current symlink
log "Updating current symlink..."
ln -sfn "$RELEASE_DIR" "$DEPLOY_ROOT/current"

# Start Apache
log "Starting Apache..."
systemctl start httpd

# Health check
log "Performing health check..."
HEALTH_URL="https://pandaserver02.sdcc.bnl.gov/swf-monitor/api/"
HTTP_STATUS=$(curl -k -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" || echo "000")

if [ "$HTTP_STATUS" = "200" ]; then
    log "✅ Health check PASSED - Application responding (HTTP $HTTP_STATUS)"
else
    log "❌ Health check FAILED - Application not responding (HTTP $HTTP_STATUS)"
    echo "WARNING: Deployment completed but application may not be working correctly"
    echo "Check Apache error logs: sudo tail -f /var/log/httpd/error_log"
    # Don't exit - deployment artifacts are in place, just alerting
fi

# Cleanup old releases (keep last 5)
log "Cleaning up old releases..."
cd "$DEPLOY_ROOT/releases"
ls -1t | tail -n +6 | xargs rm -rf 2>/dev/null || true

log "Deployment completed successfully!"
log "Active release: $DEPLOY_NAME"
log "Git commit: $(cd $RELEASE_DIR && git rev-parse --short HEAD)"

# Show status
log "Current deployment status:"
echo "  Release: $DEPLOY_NAME"
echo "  Path: $RELEASE_DIR"
echo "  Current: $(readlink $DEPLOY_ROOT/current)"