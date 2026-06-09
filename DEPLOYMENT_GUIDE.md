# Deployment Guide - WhatsApp Ticket Extraction Agent

## Overview
This guide walks through deploying the WhatsApp Ticket Extraction API to a production server with a custom domain.

---

## **Prerequisites**

- Linux server (Ubuntu 20.04+ recommended)
- Domain name pointing to your server
- Root/sudo access
- MariaDB database already configured
- Python 3.9+

---

## **Step 1: Server Setup**

### 1.1 Connect to your server
```bash
ssh root@your_server_ip
```

### 1.2 Update system packages
```bash
apt update && apt upgrade -y
```

### 1.3 Install dependencies
```bash
apt install -y python3 python3-pip python3-venv git nginx supervisor certbot python3-certbot-nginx
```

---

## **Step 2: Deploy Application**

### 2.1 Create application directory
```bash
mkdir -p /var/www/whatsapp-agent
cd /var/www/whatsapp-agent
```

### 2.2 Clone or upload your application
```bash
# Option A: If using Git
git clone https://your-repo-url.git .

# Option B: Upload files directly
# Use scp, FTP, or file transfer to upload your files
```

### 2.3 Set up Python virtual environment
```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2.4 Create .env file
```bash
nano /var/www/whatsapp-agent/.env
```

**Add your configuration:**
```env
# Database
DB_HOST=your_mariadb_host
DB_PORT=3306
DB_NAME=your_database_name
DB_USER=your_db_user
DB_PASSWORD=your_db_password

# APIs
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-proj-...

# WhatsApp (if using real-time)
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_PHONE_NUMBER_ID=...
VERIFY_TOKEN=...

# API Server
API_PORT=5001
```

### 2.5 Set permissions
```bash
chown -R www-data:www-data /var/www/whatsapp-agent
chmod -R 755 /var/www/whatsapp-agent
```

---

## **Step 3: Configure Supervisor (Process Manager)**

Supervisor keeps your application running 24/7.

### 3.1 Create supervisor config
```bash
nano /etc/supervisor/conf.d/whatsapp-agent.conf
```

**Add this configuration:**
```ini
[program:whatsapp-agent]
directory=/var/www/whatsapp-agent
command=/var/www/whatsapp-agent/venv/bin/python3 api_server.py
autostart=true
autorestart=true
stderr_logfile=/var/log/whatsapp-agent-err.log
stdout_logfile=/var/log/whatsapp-agent-out.log
user=www-data
environment=PATH="/var/www/whatsapp-agent/venv/bin"
```

### 3.2 Enable and start supervisor
```bash
supervisorctl reread
supervisorctl update
supervisorctl start whatsapp-agent
```

### 3.3 Verify it's running
```bash
supervisorctl status whatsapp-agent
```

---

## **Step 4: Configure Nginx (Reverse Proxy)**

Nginx forwards HTTP/HTTPS traffic to your Flask app.

### 4.1 Create Nginx config
```bash
nano /etc/nginx/sites-available/whatsapp-agent
```

**Add this configuration:**
```nginx
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # For file uploads (increase if needed)
        client_max_body_size 50M;
    }

    # Serve uploaded files
    location /uploads {
        alias /var/www/whatsapp-agent/uploads;
    }
}
```

### 4.2 Enable the site
```bash
ln -s /etc/nginx/sites-available/whatsapp-agent /etc/nginx/sites-enabled/
nginx -t  # Test config
systemctl restart nginx
```

---

## **Step 5: SSL Certificate (HTTPS)**

### 5.1 Install SSL certificate with Let's Encrypt
```bash
certbot --nginx -d yourdomain.com -d www.yourdomain.com
```

This will:
- ✅ Install a free SSL certificate
- ✅ Auto-renew every 90 days
- ✅ Update Nginx config automatically

### 5.2 Verify SSL renewal
```bash
certbot renew --dry-run
```

---

## **Step 6: Configure Domain DNS**

In your domain registrar's DNS settings:

| Type | Name | Value |
|------|------|-------|
| A | @ | your_server_ip |
| A | www | your_server_ip |
| CNAME | api | yourdomain.com |

Wait for DNS propagation (5-30 minutes).

---

## **Step 7: Verify Deployment**

### 7.1 Test API health
```bash
curl https://yourdomain.com/health
```

Expected response:
```json
{
  "status": "healthy",
  "service": "WhatsApp Ticket Microservice",
  "timestamp": "2026-06-09T14:30:45.123456"
}
```

### 7.2 Visit web UI
```
https://yourdomain.com
```

You should see the WhatsApp Ticket Uploader interface! 🎉

---

## **Step 8: Monitoring & Logging**

### 8.1 View application logs
```bash
tail -f /var/log/whatsapp-agent-out.log
tail -f /var/log/whatsapp-agent-err.log
```

### 8.2 View Nginx logs
```bash
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log
```

### 8.3 Monitor supervisor status
```bash
supervisorctl tail whatsapp-agent
supervisorctl status
```

---

## **Step 9: Maintenance**

### 9.1 Database backups
```bash
# Daily backup (add to crontab)
0 2 * * * mysqldump -u root -p'password' database_name > /backups/db_$(date +\%Y\%m\%d).sql
```

### 9.2 Update application
```bash
cd /var/www/whatsapp-agent
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
supervisorctl restart whatsapp-agent
```

### 9.3 View upload history
```bash
ls -lah /var/www/whatsapp-agent/uploads/
du -sh /var/www/whatsapp-agent/uploads/
```

---

## **Step 10: Firewall Configuration**

### 10.1 Allow only necessary ports
```bash
ufw allow 22/tcp   # SSH
ufw allow 80/tcp   # HTTP
ufw allow 443/tcp  # HTTPS
ufw enable
```

---

## **Troubleshooting**

### App won't start
```bash
# Check supervisor logs
tail -f /var/log/whatsapp-agent-err.log

# Restart supervisor
supervisorctl restart whatsapp-agent
```

### Database connection error
```bash
# Verify connection
python3 -c "from scheduler import get_db_connection; conn = get_db_connection(); print('✅ Connected')"
```

### SSL certificate issues
```bash
# Check certificate status
certbot certificates

# Renew certificate
certbot renew --force-renewal
```

### Nginx errors
```bash
# Check nginx config
nginx -t

# Restart nginx
systemctl restart nginx
```

---

## **Performance Tips**

1. **Enable Gzip compression** in Nginx
2. **Set up CloudFlare** for CDN (optional)
3. **Monitor disk space** for uploaded files
4. **Use connection pooling** for database
5. **Enable HTTP/2** in Nginx
6. **Set up log rotation** to prevent disk fill

---

## **Security Checklist**

- ✅ Use HTTPS only
- ✅ Strong database passwords
- ✅ Firewall configured
- ✅ Regular backups
- ✅ Keep system updated
- ✅ Monitor access logs
- ✅ Use env variables (not hardcoded secrets)
- ✅ Limit file upload size
- ✅ Regular security audits

---

## **Success!**

Your application is now live on:
```
https://yourdomain.com
```

**Next steps:**
1. Share the domain with your team
2. Test uploads with real data
3. Monitor logs and database
4. Set up automated backups
5. Plan regular maintenance

---

**Need help?** Check logs or contact your server provider.
