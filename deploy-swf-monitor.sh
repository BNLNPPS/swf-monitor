#!/bin/bash
#
# SWF Monitor Deployment Script
# Usage: deploy-swf-monitor.sh [tag|branch] <reference>
#
# Examples:
#   deploy-swf-monitor.sh tag tagName
#   deploy-swf-monitor.sh branch infra/baseline-v18
#   deploy-swf-monitor.sh branch main
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

# Create production environment file if it doesn't exist
if [ ! -f "$DEPLOY_ROOT/config/env/production.env" ]; then
    log "Creating production environment configuration..."
    cat > "$DEPLOY_ROOT/config/env/production.env" << 'EOF'
# Production Environment Configuration for SWF Monitor
DEBUG=False
SECRET_KEY=your-production-secret-key-here-change-this
SWF_ALLOWED_HOSTS=localhost,127.0.0.1,pandasserver02.sdcc.bnl.gov
SWF_MONITOR_URL=http://localhost
SWF_MONITOR_HTTP_URL=http://localhost
DB_HOST=localhost
DB_NAME=swfdb
DB_USER=wenaus
# Add other production-specific environment variables here
EOF
    log "Created default production.env - please review and update values"
fi

# Link shared resources
log "Linking shared resources..."
ln -sf "$DEPLOY_ROOT/shared/logs" "$RELEASE_DIR/logs"
ln -sf "$DEPLOY_ROOT/config/env/production.env" "$RELEASE_DIR/.env"

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

# Update current symlink
log "Updating current symlink..."
ln -sfn "$RELEASE_DIR" "$DEPLOY_ROOT/current"

# Set ownership
log "Setting ownership..."
chown -R "$CURRENT_USER:eic" "$DEPLOY_ROOT"

# Reload Apache
log "Reloading Apache..."
systemctl reload httpd

# Health check
log "Performing health check..."
HEALTH_URL="http://localhost/swf-monitor/"
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" || echo "000")

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