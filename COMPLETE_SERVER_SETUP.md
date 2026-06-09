# Complete Server Setup Guide - From Scratch

This guide will walk you through deploying your WhatsApp Ticket Agent from your local machine to a live server with a domain.

---

## **What You Have Right Now**

On your local machine: `/Users/rohanamir/Desktop/Support_Agent/`

```
Support_Agent/
├── scheduler.py          (main extraction agent)
├── api_server.py         (Flask web server)
├── upload_client.py      (upload tool)
├── upload_ui.html        (web interface)
├── monitor_db.py         (database monitoring)
├── requirements.txt      (Python dependencies)
├── config.json           (configuration)
├── .env                  (your credentials - DON'T UPLOAD THIS!)
└── .env.example          (template - upload this)
```

---

## **What You Need**

1. ✅ **Domain name** — yourdomain.com (already have)
2. ✅ **Linux server** — Ubuntu 20.04+ (need to get from hosting provider)
3. ✅ **MariaDB database** — Already set up at pm.lucrumerp.com
4. ✅ **SSH access** — To connect to your server

---

## **Part 1: Get a Server**

### Step 1.1: Choose a Hosting Provider
Popular options:
- **DigitalOcean** ($5-10/month) - Easy to use
- **AWS** (variable pricing) - Scalable
- **Linode** ($5-30/month) - Good performance
- **Vultr** ($2.50-5/month) - Cheap
- **Contabo** ($3-4/month) - Very affordable

### Step 1.2: Create a server instance
- Select: Ubuntu 20.04 LTS
- Size: 1GB RAM minimum (2GB recommended)
- Region: Choose closest to your location
- Get your **server IP address** (e.g., `123.45.67.89`)

### Step 1.3: Point your domain to the server
In your domain registrar (GoDaddy, Namecheap, etc.):

1. Go to **DNS Settings**
2. Find **A Record**
3. Change the value to your **server IP**: `123.45.67.89`
4. Wait 5-30 minutes for DNS to update

Test if it's working:
```bash
ping yourdomain.com
# Should return your server IP
```

---

## **Part 2: Prepare Your Local Files**

### Step 2.1: Create a clean folder with only deployment files

On your local machine, create a folder:
```bash
mkdir ~/Desktop/Support_Agent_Deploy
cd ~/Desktop/Support_Agent_Deploy
```

### Step 2.2: Copy these files from `/Users/rohanamir/Desktop/Support_Agent/`

```
Support_Agent_Deploy/
├── scheduler.py
├── api_server.py
├── upload_client.py
├── upload_ui.html
├── monitor_db.py
├── test_db_connection.py
├── requirements.txt
├── config.json
└── .env.example         (NOT .env! Never upload real .env)
```

### Step 2.3: Create a new `.env.production` file
```bash
# Create new file with your production database details
cat > ~/.ssh/env.production << 'EOF'
DB_HOST=pm.lucrumerp.com
DB_PORT=3306
DB_NAME=_94b2ff7162645e02
DB_USER=rohan
DB_PASSWORD=lucrum#*%&!168!

ANTHROPIC_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here

API_PORT=5001
EOF
```

**Store this file safely** - you'll need it during server setup.

---

## **Part 3: Connect to Your Server**

### Step 3.1: Open Terminal and SSH into server

```bash
# Replace 123.45.67.89 with your server IP
ssh root@123.45.67.89

# First time connecting? Type 'yes' when asked about fingerprint
```

### Step 3.2: Verify you're connected

You should see a prompt like:
```
root@server-name:~#
```

If you can see this, you're in! ✅

---

## **Part 4: Setup Server Environment**

### Step 4.1: Update server packages

Copy-paste this into your SSH terminal:
```bash
apt update && apt upgrade -y
```

Wait for it to finish (takes 1-2 minutes).

### Step 4.2: Install required software

```bash
apt install -y python3 python3-pip python3-venv git nginx supervisor certbot python3-certbot-nginx curl
```

Wait for installation to complete.

### Step 4.3: Verify Python is installed

```bash
python3 --version
pip3 --version
```

Both should show version numbers.

---

## **Part 5: Upload Your Application**

You have 2 options:

### **Option A: Using SCP (Simple)**

On your **local machine** (NOT on the server), run:

```bash
# Copy all files to server
scp -r ~/Desktop/Support_Agent_Deploy/* root@123.45.67.89:/var/www/whatsapp-agent/

# Copy .env.production as .env on server
scp ~/.ssh/env.production root@123.45.67.89:/var/www/whatsapp-agent/.env
```

### **Option B: Using Git (If using GitHub)**

On the **server** (in your SSH terminal), run:

```bash
git clone https://github.com/yourusername/support-agent.git /var/www/whatsapp-agent
cd /var/www/whatsapp-agent
```

---

## **Part 6: Prepare the Server Directory**

In your **SSH terminal**, run these commands:

### Step 6.1: Create the application directory (if using SCP)

```bash
# If you already uploaded files via SCP, you can skip this
# If using Git, files are already there
```

### Step 6.2: Set up Python virtual environment

```bash
cd /var/www/whatsapp-agent
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

This takes 2-3 minutes.

### Step 6.3: Verify your .env file is there

```bash
ls -la /var/www/whatsapp-agent/.env
cat /var/www/whatsapp-agent/.env | head -5
```

You should see your database credentials.

### Step 6.4: Test database connection

```bash
cd /var/www/whatsapp-agent
source venv/bin/activate
python3 test_db_connection.py
```

You should see:
```
✅ Database connection OK
Found 821 table(s)
✅ whatsapp_raw_message table exists
✅ tabPulse Support Ticket table exists
```

**If this passes, you're good to go!** ✅

---

## **Part 7: Set File Permissions**

In your **SSH terminal**:

```bash
chown -R www-data:www-data /var/www/whatsapp-agent
chmod -R 755 /var/www/whatsapp-agent
```

---

## **Part 8: Setup Supervisor (Keeps App Running)**

### Step 8.1: Create supervisor config file

In your **SSH terminal**:

```bash
nano /etc/supervisor/conf.d/whatsapp-agent.conf
```

This opens a text editor. **Copy-paste this entire block:**

```
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

Press **Ctrl+X**, then **Y**, then **Enter** to save and exit.

### Step 8.2: Start supervisor

```bash
supervisorctl reread
supervisorctl update
supervisorctl start whatsapp-agent
supervisorctl status whatsapp-agent
```

You should see:
```
whatsapp-agent                   RUNNING   pid 1234, uptime 0:00:05
```

**If you see RUNNING, your app is live!** ✅

---

## **Part 9: Setup Nginx (Web Server)**

### Step 9.1: Create Nginx config

```bash
nano /etc/nginx/sites-available/whatsapp-agent
```

**Copy-paste this entire block:**

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
        client_max_body_size 50M;
    }

    location /uploads {
        alias /var/www/whatsapp-agent/uploads;
    }
}
```

**Replace `yourdomain.com`** with your actual domain!

Press **Ctrl+X**, then **Y**, then **Enter** to save.

### Step 9.2: Enable the site

```bash
ln -s /etc/nginx/sites-available/whatsapp-agent /etc/nginx/sites-enabled/
nginx -t
systemctl restart nginx
```

---

## **Part 10: Setup HTTPS (SSL Certificate)**

```bash
# Get free certificate from Let's Encrypt
certbot --nginx -d yourdomain.com -d www.yourdomain.com

# Follow prompts - enter your email
# When asked "redirect to HTTPS" - choose YES (option 2)
```

After this, your site will be **HTTPS only** ✅

---

## **Part 11: Test Your Application**

### From your **local machine**, open a browser:

```
https://yourdomain.com
```

You should see: **📱 WhatsApp Ticket Uploader**

### Test the API health:

```bash
curl https://yourdomain.com/health
```

You should see:
```json
{
  "status": "healthy",
  "service": "WhatsApp Ticket Microservice",
  "timestamp": "2026-06-09T14:30:45.123456"
}
```

---

## **Part 12: View Logs**

If something doesn't work, check the logs on your **server**:

```bash
# Application errors
tail -f /var/log/whatsapp-agent-err.log

# Application output
tail -f /var/log/whatsapp-agent-out.log

# Web server errors
tail -f /var/log/nginx/error.log
```

---

## **Summary: Directory Structure on Server**

After setup, your server looks like:

```
/var/www/whatsapp-agent/
├── venv/                  (Python virtual environment)
├── uploads/               (User uploads - created automatically)
├── scheduler.py
├── api_server.py
├── upload_client.py
├── upload_ui.html
├── .env                   (Your database credentials)
├── config.json
├── requirements.txt
└── (other files)
```

---

## **Your Application is Now Live!**

### Access it at:
```
https://yourdomain.com
```

### Share with your team:
```
"Upload WhatsApp chat exports at https://yourdomain.com"
```

### Monitor from your local machine:
```bash
# SSH in anytime to check logs
ssh root@123.45.67.89
tail -f /var/log/whatsapp-agent-out.log
```

---

## **Troubleshooting**

### ❌ "Connection refused" / "Upstream server failed"
```bash
# Check if app is running
supervisorctl status whatsapp-agent
# Should show RUNNING

# If not, check errors
tail -f /var/log/whatsapp-agent-err.log
```

### ❌ "Database connection failed"
```bash
# Verify database credentials in .env
cat /var/www/whatsapp-agent/.env

# Test connection
python3 /var/www/whatsapp-agent/test_db_connection.py
```

### ❌ "Domain not found"
```bash
# Give DNS time to update (5-30 minutes)
# Or check: nslookup yourdomain.com
```

### ❌ "HTTPS not working"
```bash
# Check certificate
certbot certificates

# Renew if needed
certbot renew
```

---

## **Quick Command Reference**

| Task | Command |
|------|---------|
| Connect to server | `ssh root@123.45.67.89` |
| Check app status | `supervisorctl status whatsapp-agent` |
| Restart app | `supervisorctl restart whatsapp-agent` |
| View app logs | `tail -f /var/log/whatsapp-agent-out.log` |
| Restart web server | `systemctl restart nginx` |
| Check domain DNS | `nslookup yourdomain.com` |
| Test API | `curl https://yourdomain.com/health` |

---

**Done! Your application is now live and accessible worldwide!** 🚀
