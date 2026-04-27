# Production Deployment Guide

**Complete guide for deploying SWF Monitor to production Apache environment on pandaserver02.**

## Overview

This guide covers the complete production deployment process for the SWF Monitor Django application, including initial infrastructure setup and ongoing deployment updates. The production environment uses Apache with Python 3.11 mod_wsgi to serve the Django application.

Note on usage modes:
- Standalone development mode runs local services under supervisord for convenience.
- Production platform mode relies on central, system-managed services (PostgreSQL, ActiveMQ, Redis, Apache).
Use the system status reporter to see which mode your host supports and which services are available:

```bash
python /eic/u/wenauseic/github/swf-testbed/report_system_status.py
```

## Architecture

**Production Structure:**
```
/opt/swf-monitor/
├── releases/           # Versioned deployments
│   ├── branch-main/
│   └── branch-infra-baseline-vX/
├── current/           # Symlink to active release
├── shared/
│   ├── logs/          # Application logs
│   ├── static/        # Django static files
│   └── uploads/       # File uploads
├── config/
│   ├── apache/        # Apache configuration
│   └── env/
│       └── production.env  # Production environment variables
└── bin/
    └── deploy-swf-monitor.sh  # Deployment automation
```

**Key Components:**
- **Apache HTTP Server**: Serves static files, proxies Django via mod_wsgi for most paths, and ProxyPasses `/swf-monitor/mcp/` to the ASGI worker
- **Python 3.11 mod_wsgi**: WSGI interface for Django application (all paths except `/mcp/`)
- **ASGI worker (uvicorn)**: `swf-monitor-mcp-asgi.service` on `127.0.0.1:8001` serves `/swf-monitor/mcp/` as stateless POST request/response MCP. The ASGI worker isolates MCP failures from the rest of the app.
- **PostgreSQL**: Production database (system-managed)
- **ActiveMQ**: Message broker (system-managed via artemis.service)
- **Redis (Channels layer)**: Required inter-process relay used by the SSE forwarder. Redis/Channels-backed SSE is an integral part of the system whenever remote ActiveMQ client recipients are supported.
- **Mattermost bots**: `swf-panda-bot.service` and `swf-testbed-bot.service` — Claude-backed chatbots for `#pandabot` and `#testbed-bot` channels
- **Release Management**: Automated deployment with Apache-conf sync and ASGI-worker recycle

## Prerequisites

Before starting production deployment, ensure:

1. **System Services Running:**
   - PostgreSQL (postgresql-16.service or equivalent)
   - ActiveMQ/Artemis (artemis.service)
   - Apache HTTP Server (httpd.service)
   - Redis (redis.service) — Required for SSE relay via Django Channels. This is integral to production operation to support remote recipients of ActiveMQ events over HTTPS (SSE).

2. **Development Environment Ready:**
   - SWF testbed development environment set up (see [Development Environment Setup](#development-environment-setup) below)
   - Virtual environment with all dependencies installed
   - All repositories updated to desired branch/tag

3. **System Access:**
   - Root access (sudo) for Apache configuration and deployment
   - Database credentials for production PostgreSQL instance

### Development Environment Setup

To set up an equivalent development environment in any user account:

1. **Clone all repositories as siblings:**
   ```bash
   cd /path/to/your/workspace
   git clone https://github.com/BNLNPPS/swf-testbed.git
   git clone https://github.com/BNLNPPS/swf-monitor.git
   git clone https://github.com/BNLNPPS/swf-common-lib.git
   # Clone other swf-* agent repositories as needed
   ```

2. **Set up the testbed environment:**
   ```bash
   cd swf-testbed
   source install.sh  # Creates .venv and installs all dependencies
   ```

3. **Configure environment variables:**
   ```bash
   # Copy and customize environment template
   cp ../swf-monitor/.env.example ~/.env
   # Edit ~/.env with your specific configuration
   ```

4. **Set up database (if using local PostgreSQL):**
   ```bash
   cd swf-monitor/src
   source /path/to/swf-testbed/.venv/bin/activate
   python manage.py migrate
   python manage.py createsuperuser
   ```

The production deployment will copy the virtual environment from your development setup, so ensure all required packages are installed in your development `.venv`.

## Initial Production Setup

**⚠️ This setup is performed ONCE when initially installing the production environment.**

### Step 1: Run Apache Deployment Setup

```bash
# From the swf-monitor repository root
sudo ./setup-apache-deployment.sh
```

This automated setup script:

1. **Creates deployment structure** at `/opt/swf-monitor/`
2. **Installs Apache development headers** (httpd-devel)
3. **Compiles Python 3.11 mod_wsgi** in the project virtual environment
4. **Disables system mod_wsgi** to avoid Python version conflicts
5. **Generates LoadModule config** into `/etc/httpd/conf.modules.d/20-swf-monitor-wsgi.conf` (LoadModule + WSGIPythonHome only — loads before `conf.d/` so the module is available when WSGIDaemonProcess is parsed)
6. **Installs the Apache vhost config** by copying the repo canonical `apache-swf-monitor.conf` to `/etc/httpd/conf.d/swf-monitor.conf`. The repo file is the source of truth; `deploy-swf-monitor.sh` keeps live in sync with it on every deploy.
7. **Copies deployment automation** script to `/opt/swf-monitor/bin/`
8. **Creates production environment** template
9. **Tests Apache configuration** (`httpd -t`) and restarts Apache

Note: you must also install the `swf-monitor-mcp-asgi.service` systemd unit from the repo (not automated by this script; one-time bootstrap per host):

```bash
sudo install -o root -g root -m 644 swf-monitor-mcp-asgi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now swf-monitor-mcp-asgi.service
```

Optional but recommended: install the MCP watchdog timer, which probes the
non-MCP health endpoint plus MCP `initialize` and `tools/list`, and restarts
the ASGI worker if the probe fails:

```bash
sudo install -o root -g root -m 644 swf-monitor-mcp-watchdog.service /etc/systemd/system/
sudo install -o root -g root -m 644 swf-monitor-mcp-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now swf-monitor-mcp-watchdog.timer
```

### Step 2: Configure Production Environment

Review and update the production environment configuration:

```bash
sudo nano /opt/swf-monitor/config/env/production.env
```

**Key Configuration Categories:**

- **Security Settings**: `DEBUG=False`, `SECRET_KEY`, `SWF_ALLOWED_HOSTS`
- **Host Configuration**: URLs and hostnames for the production server
- **Database Configuration**: PostgreSQL connection details
- **ActiveMQ Configuration**: Message broker settings and SSL certificates
- **Redis/Channels Configuration**: `REDIS_URL` for Channels channel layer powering SSE relay. Required for remote ActiveMQ recipients; without it, only single-process dev streaming works and is not suitable for production.
- **API Authentication**: Tokens for agent authentication
- **Proxy Settings**: Network proxy configuration

**Note**: The production.env file contains sensitive configuration values that must be customized for your environment.

**⚠️ IMPORTANT: .env is NOT deployed from git.** The `.env` file is in `.gitignore` for security reasons. The deploy script symlinks the release `.env` to `/opt/swf-monitor/config/env/production.env`. When you need to change environment settings (e.g., `ACTIVEMQ_HEARTBEAT_TOPIC`), you must edit the production.env file directly:

```bash
sudo nano /opt/swf-monitor/config/env/production.env
# Then restart Apache to pick up changes:
sudo systemctl restart httpd
```

### Step 3: Deploy First Release

Deploy your first production release:

```bash
# Deploy main branch
sudo /opt/swf-monitor/bin/deploy-swf-monitor.sh branch main

# OR deploy specific infrastructure branch
sudo /opt/swf-monitor/bin/deploy-swf-monitor.sh branch infra/baseline-v18
```

### Step 4: Verify Deployment

Test the production deployment:

```bash
# Test HTTP access
curl https://pandaserver02.sdcc.bnl.gov/swf-monitor/

# Check Apache status
systemctl status httpd

# Check deployment status
ls -la /opt/swf-monitor/current
```

## Ongoing Deployment Updates

**For regular updates to the production environment.**

### Standard Update Process

When your repositories are ready for production update:

```bash
# Deploy main branch (most common)
sudo /opt/swf-monitor/bin/deploy-swf-monitor.sh branch main

# Deploy specific branch
sudo /opt/swf-monitor/bin/deploy-swf-monitor.sh branch infra/baseline-v19

# Deploy specific tag
sudo /opt/swf-monitor/bin/deploy-swf-monitor.sh tag v1.2.3
```

### What Happens During Deployment

The deployment script automatically:

1. **Validates** the branch/tag exists in GitHub repository
2. **Creates** new release directory: `/opt/swf-monitor/releases/branch-main/`
3. **Clones** the specified Git reference to the release directory
4. **Copies** development virtual environment from the configured development path
5. **Links** shared resources (logs, production.env, SSL certificates) and ensures the shared HuggingFace cache exists with open perms
6. **Installs** WSGI LoadModule config from release's `config/apache/20-swf-monitor-wsgi.conf` into `/etc/httpd/conf.modules.d/`
7. **Collects** Django static files with `python manage.py collectstatic`
8. **Syncs** static files to shared Apache location
9. **Runs** database migrations with `python manage.py migrate`
10. **Updates** current symlink to point to new release, sets proper ownership
11. **Syncs Apache vhost conf** — compares release's `apache-swf-monitor.conf` with live `/etc/httpd/conf.d/swf-monitor.conf`; if different, timestamped backup + install + `httpd -t` validates, rollback on failure
12. **Reloads** Apache (`systemctl reload httpd`) — required every deploy to recycle mod_wsgi daemon processes so they pick up new Python code; any conf change from step 11 rides along on the same reload
13. **Restarts** the ASGI worker (`systemctl restart swf-monitor-mcp-asgi.service`) so uvicorn picks up new code (uvicorn loads code once at startup and does not re-read on file change)
14. **Conditionally restarts bots** (`swf-panda-bot`, `swf-testbed-bot`) — only if bot-specific code changed relative to the previous release
15. **Health-checks** the deployment by hitting `/swf-monitor/api/`
16. **Cleans up** old releases (keeps last 5)


### Deployment Output

Successful deployment shows:

```
[2025-01-13 14:30:15] Deployment completed successfully!
[2025-01-13 14:30:15] Active release: branch-main
[2025-01-13 14:30:15] Git commit: a1b2c3d

Current deployment status:
  Release: branch-main
  Path: /opt/swf-monitor/releases/branch-main
  Current: /opt/swf-monitor/releases/branch-main
```

## Apache Configuration

**Source of truth:** `apache-swf-monitor.conf` in the repo root. The deploy script copies it to `/etc/httpd/conf.d/swf-monitor.conf` on every release whenever it differs from live (with `httpd -t` validation + rollback on failure). Editing the live file directly is safe for emergency triage, but any deploy will re-install the repo canonical — so durable changes belong in the repo file.

**Two-backend layout:**
- mod_wsgi (`WSGIDaemonProcess swf-monitor`) serves `/swf-monitor/*` **except** `/mcp/`
- mod_proxy → ASGI (uvicorn on `127.0.0.1:8001`) serves `/swf-monitor/mcp/` only

Key directives (abridged — see `apache-swf-monitor.conf` for the full file):

```apache
# WSGI tuning — threads absorb bursty concurrency; listen-backlog absorbs retry
# bursts; queue/inactivity/graceful timeouts bound failure modes. No
# request-timeout because it would truncate /api/messages/stream/ SSE long-poll.
WSGIDaemonProcess swf-monitor \
    python-path=/opt/swf-monitor/current/src:/opt/swf-monitor/current/.venv/lib/python3.11/site-packages \
    python-home=/opt/swf-monitor/current/.venv \
    processes=1 threads=30 \
    listen-backlog=500 queue-timeout=30 \
    inactivity-timeout=300 graceful-timeout=15 \
    display-name=%{GROUP} lang='en_US.UTF-8' locale='en_US.UTF-8'

SetEnv SWF_HOME /opt/swf-monitor

# MCP on ASGI worker — stateless POST request/response MCP.
# Must appear BEFORE WSGIScriptAlias so the proxy takes precedence for /mcp/.
<Location /swf-monitor/mcp/>
    ProxyPass         http://127.0.0.1:8001/swf-monitor/mcp/ timeout=60 keepalive=On disablereuse=On
    ProxyPassReverse  http://127.0.0.1:8001/swf-monitor/mcp/
    RequestHeader set X-Forwarded-Proto "https"
    CacheDisable on
</Location>

WSGIScriptAlias /swf-monitor /opt/swf-monitor/current/src/swf_monitor_project/wsgi.py process-group=swf-monitor
WSGIPassAuthorization On

Alias /swf-monitor/static /opt/swf-monitor/shared/static

<Location /swf-monitor>
    Header always set X-Content-Type-Options nosniff
    Header always set X-Frame-Options DENY
    Header always set X-XSS-Protection "1; mode=block"
</Location>
```

**LoadModule** is in a separate file (`/etc/httpd/conf.modules.d/20-swf-monitor-wsgi.conf`) generated by `setup-apache-deployment.sh` at bootstrap and re-installed from the repo's `config/apache/` on every deploy. The prefix `conf.modules.d/` (vs `conf.d/`) matters — Apache loads that directory first, so the module is available when `WSGIDaemonProcess` is parsed.

## Service Management

### Apache Control

```bash
# Reload configuration (for deployments)
sudo systemctl reload httpd

# Restart Apache (for configuration changes)
sudo systemctl restart httpd

# Check Apache status
sudo systemctl status httpd

# View Apache logs
sudo tail -f /var/log/httpd/error_log
sudo tail -f /var/log/httpd/access_log
```

### ASGI Worker (MCP endpoint)

`/swf-monitor/mcp/` is served by `swf-monitor-mcp-asgi.service`, a uvicorn ASGI worker bound to `127.0.0.1:8001`. Apache ProxyPasses to it.

```bash
# Restart (picks up new Python code — uvicorn does not re-read files)
sudo systemctl restart swf-monitor-mcp-asgi.service

# Status
sudo systemctl status swf-monitor-mcp-asgi.service

# Logs
sudo journalctl -u swf-monitor-mcp-asgi.service -f
```

The deploy script restarts this unit on every deploy; manual restart is only needed for targeted code changes or recovery from a crash-loop. The MCP endpoint is operated as stateless POST request/response MCP on this host; long-lived GET/SSE streaming is not an operational dependency.

Health checks:

```bash
# Non-MCP health endpoint: verifies Django and default DB connectivity
curl http://127.0.0.1:8001/swf-monitor/api/mcp-health/

# Full local watchdog probe without restart
/opt/swf-monitor/current/.venv/bin/python /opt/swf-monitor/current/scripts/mcp_watchdog.py
```

### MCP Watchdog

If installed, `swf-monitor-mcp-watchdog.timer` runs once per minute and invokes
`scripts/mcp_watchdog.py --restart`. The probe checks:

1. `/swf-monitor/api/mcp-health/`
2. MCP `initialize`
3. MCP `tools/list`

Failures are visible in the timer/service journal. The watchdog is deliberately
an external HTTP probe rather than an in-process Django management command.

### Mattermost Bots

```bash
sudo systemctl restart swf-panda-bot.service
sudo systemctl restart swf-testbed-bot.service
sudo journalctl -u swf-panda-bot.service -f
```

### Application Logs

```bash
# SWF Monitor application logs
sudo tail -f /opt/swf-monitor/shared/logs/swf-monitor.log

# Django debug logs (if DEBUG=True)
sudo tail -f /opt/swf-monitor/current/src/debug.log
```


## Troubleshooting

### Common Issues

**1. Apache won't start:**
```bash
# Check Apache configuration
sudo httpd -t

# Check mod_wsgi module load
sudo httpd -M | grep wsgi
```

**2. Python module errors:**
```bash
# Verify virtual environment
ls -la /opt/swf-monitor/current/.venv/

# Check Python path in Apache error log
sudo tail -f /var/log/httpd/error_log
```

**3. Database connection errors:**
```bash
# Test database connectivity from production.env values
# Check production.env configuration
sudo cat /opt/swf-monitor/config/env/production.env
```

**4. Static files not loading:**
```bash
# Check static files location
ls -la /opt/swf-monitor/shared/static/

# Recollect static files
cd /opt/swf-monitor/current/src
sudo python manage.py collectstatic --clear --noinput
```

**5. Permission errors:**
```bash
# Fix ownership (adjust user:group as needed)
sudo chown -R [user]:[group] /opt/swf-monitor/

# Fix Apache static file permissions
sudo chmod -R 755 /opt/swf-monitor/shared/static/
```

### Diagnostic Commands

```bash
# Check deployment status
ls -la /opt/swf-monitor/current
readlink /opt/swf-monitor/current

# Check Apache mod_wsgi status
sudo httpd -M | grep wsgi

# Test application directly
cd /opt/swf-monitor/current/src
source /opt/swf-monitor/current/.venv/bin/activate
python manage.py check --deploy

# Check all services
sudo systemctl status httpd swf-monitor-mcp-asgi swf-panda-bot swf-testbed-bot postgresql-16 artemis redis
```

## Security Considerations

### Production Checklist

- [ ] `DEBUG=False` in production.env
- [ ] `SWF_ALLOWED_HOSTS` properly configured
- [ ] Strong `SECRET_KEY` set
- [ ] Database credentials secured
- [ ] SSL certificates properly configured
- [ ] File permissions properly set (755 for directories, 644 for files)
- [ ] Production.env file permissions restrictive (600)

### SSL/TLS Configuration

The SWF Monitor works with the existing Apache SSL setup. SSL configuration is handled by the system's ssl.conf file, not the swf-monitor specific configuration.

### File Permissions

```bash
# Correct ownership (adjust user:group as needed)
sudo chown -R [user]:[group] /opt/swf-monitor/

# Secure environment file
sudo chmod 600 /opt/swf-monitor/config/env/production.env

# Apache-accessible static files
sudo chmod -R 755 /opt/swf-monitor/shared/static/
```

## Monitoring and Maintenance

### Regular Maintenance

1. **Monitor disk space** in `/opt/swf-monitor/releases/` (automatic cleanup keeps 5 releases)
2. **Check Apache error logs** regularly
3. **Monitor database growth** and performance
4. **Update SSL certificates** when needed
5. **Keep development environment updated** (deployment copies .venv from dev)

### Performance Monitoring

```bash
# Check Apache processes
ps aux | grep httpd

# Monitor database connections
# Use appropriate database monitoring commands for your setup

# Check system resources
htop
df -h /opt/
```

## Development Environment Impact

**Important:** The production deployment system copies the virtual environment from your development setup.

This means:
- Keep your development environment updated with production requirements
- Test thoroughly in development before deploying
- Development and production use the same Python packages and versions
- Your development environment remains unchanged and accessible

## Support and Documentation

- **Main Documentation**: [swf-monitor README](../README.md)
- **Development Guide**: [SETUP_GUIDE.md](SETUP_GUIDE.md)
- **API Documentation**: [API_REFERENCE.md](API_REFERENCE.md)
- **Parent Project**: [swf-testbed documentation](../../swf-testbed/README.md)

---

*For urgent production issues, check Apache error logs first: `sudo tail -f /var/log/httpd/error_log`*
