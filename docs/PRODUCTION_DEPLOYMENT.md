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
- **Apache HTTP Server**: Serves static files and proxies Django via mod_wsgi
- **Python 3.11 mod_wsgi**: WSGI interface for Django application
- **PostgreSQL**: Production database (system-managed)
- **ActiveMQ**: Message broker (system-managed via artemis.service)
 - **Redis (Channels layer)**: Required inter-process relay used by the SSE forwarder. Redis/Channels-backed SSE is an integral part of the system whenever remote ActiveMQ client recipients are supported.
- **Release Management**: Automated deployment

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
4. **Generates Apache configuration** with Python 3.11 mod_wsgi paths
5. **Disables system mod_wsgi** to avoid Python version conflicts
6. **Installs Apache virtual host** configuration at `/etc/httpd/conf.d/swf-monitor.conf`
7. **Copies deployment automation** script to `/opt/swf-monitor/bin/`
8. **Creates production environment** template
9. **Tests Apache configuration** and restarts Apache

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
5. **Links** shared resources (logs, production.env, SSL certificates)
6. **Collects** Django static files with `python manage.py collectstatic`
7. **Syncs** static files to shared Apache location
8. **Runs** database migrations with `python manage.py migrate`
9. **Updates** current symlink to point to new release
10. **Sets** proper file ownership and permissions
11. **Reloads** Apache configuration (`systemctl reload httpd`)
12. **Cleans up** old releases (keeps last 5)


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

**Location:** `/etc/httpd/conf.d/swf-monitor.conf`

The production Apache configuration:

```apache
# Load Python 3.11 mod_wsgi module (auto-generated paths)
LoadModule wsgi_module "/path/to/venv/lib/python3.11/site-packages/mod_wsgi/server/mod_wsgi-py311.cpython-311-x86_64-linux-gnu.so"
WSGIPythonHome "/path/to/development/.venv"

# WSGI Daemon Process
WSGIDaemonProcess swf-monitor \
    python-path=/opt/swf-monitor/current/src:/opt/swf-monitor/current/.venv/lib/python3.11/site-packages \
    python-home=/opt/swf-monitor/current/.venv \
    processes=2 \
    threads=15 \
    display-name=%{GROUP} \
    lang='en_US.UTF-8' \
    locale='en_US.UTF-8'

# URL Mapping
WSGIScriptAlias /swf-monitor /opt/swf-monitor/current/src/swf_monitor_project/wsgi.py process-group=swf-monitor

# Static Files (served directly by Apache)
Alias /swf-monitor/static /opt/swf-monitor/shared/static

# Security Headers
<Location /swf-monitor>
    Header always set X-Content-Type-Options nosniff
    Header always set X-Frame-Options DENY  
    Header always set X-XSS-Protection "1; mode=block"
</Location>
```

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
sudo systemctl status httpd postgresql-16 artemis redis
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