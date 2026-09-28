# 📘 Panduan Lengkap Deployment PingOn (GitHub ➔ Ubuntu Server)

Dokumentasi ini menjelaskan langkah demi langkah cara meng-upload source code **PingOn** ke GitHub hingga menjalankannya secara online di server **Linux Ubuntu** menggunakan **Docker Compose** atau **Systemd Service**.

---

## 📑 Daftar Isi
1. [Arsitektur & Fitur Aplikasi](#1-arsitektur--fitur-aplikasi)
2. [Persiapan & Push Source Code ke GitHub](#2-persiapan--push-source-code-ke-github)
3. [Persiapan Server Ubuntu & Firewall](#3-persiapan-server-ubuntu--firewall)
4. [Metode 1: Deployment via Docker Compose (Direkomendasikan)](#4-metode-1-deployment-via-docker-compose-direkomendasikan)
5. [Metode 2: Deployment Native via Python Systemd Service](#5-metode-2-deployment-native-via-python-systemd-service)
6. [Konfigurasi Domain & HTTPS SSL (Nginx + Let's Encrypt)](#6-konfigurasi-domain--https-ssl-nginx--lets-encrypt)
7. [Akses Pertama Kali & Pengaturan Notifikasi Telegram](#7-akses-pertama-kali--pengaturan-notifikasi-telegram)
8. [Perawatan & Update Aplikasi (Maintenance)](#8-perawatan--update-aplikasi-maintenance)

---

## 1. Arsitektur & Fitur Aplikasi

- **Backend**: Python 3.12, FastAPI, SQLite, Uvicorn.
- **Frontend**: Single-page application vanilla JS, Tailwind CSS, Glassmorphic Terminal Login UI, uPlot & Lucide Icons.
- **Security**: JWT Authentication (OAuth2 Bearer), bcrypt password hashing, Role-Based Access Control (Admin & Guest).
- **Monitoring**: Native ICMP Ping Subprocess (non-blocking asyncio task per target), Flapping protection, Latency RTT & Packet Loss measurement.
- **Notification**: Telegram Bot Alert otomatis saat target `DOWN` atau `UP` (Recovery).
- **Backup**: Otomatis backup SQLite database harian dengan retensi terjadwal.

---

## 2. Persiapan & Push Source Code ke GitHub

### A. Pastikan File `.gitignore` Ada
Pastikan file `.gitignore` ada di root folder project agar file kredensial (`.env`) dan database lokal (`*.db`) tidak ter-upload ke publik:
```gitignore
__pycache__/
*.py[cod]
.env
backend/.env
backend/data/*.db
backend/data/backups/
.pytest_cache/
venv/
.venv/
```

### B. Buat Repository Baru di GitHub
1. Buka [github.com/new](https://github.com/new).
2. Beri nama repository, misalnya `pingon` atau `ping-monitor`.
3. Pilih **Private** atau **Public**.
4. Biarkan opsi *"Initialize with README/gitignore"* **kosong** (jangan dicentang).
5. Klik **Create repository**.

### C. Buat Personal Access Token (PAT) di GitHub
*Karena GitHub tidak menerima password akun biasa untuk push:*
1. Masuk ke **Settings** ➔ **Developer Settings** ➔ **Personal access tokens** ➔ **Tokens (classic)**.
2. Klik **Generate new token (classic)**.
3. Beri nama (contoh: `server-deploy`), tentukan masa berlaku, dan centang hak akses **`repo`**.
4. Klik **Generate token**, lalu salin token yang muncul (format: `ghp_...`).

### D. Inisialisasi & Push ke GitHub
Di terminal komputer lokal Anda (folder project `ping-monitor`):
```bash
git init -b main
git add .
git commit -m "feat: initial release PingOn dashboard"
git remote add origin https://github.com/USERNAME_ANDA/pingon.git
git push -u origin main
```
*Saat diminta password di terminal, masukkan token `ghp_...` yang telah Anda salin.*

---

## 3. Persiapan Server Ubuntu & Firewall

### A. Update Paket OS
Login ke server via SSH:
```bash
ssh root@IP_SERVER_ANDA
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl ufw
```

### B. Buka Port di Firewall Ubuntu (UFW)
```bash
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS
sudo ufw allow 8000/tcp  # Port Default PingOn
sudo ufw --force enable
sudo ufw status
```

### C. Buka Security Group di Cloud Panel (Jika Memakai Cloud/VPS)
Jika Anda menggunakan provider VPS (seperti **IDCloudHost, Biznet GIO, Alibaba Cloud, AWS, GCP, DigitalOcean**):
1. Buka dashboard web provider VPS Anda.
2. Masuk ke menu **Firewall** / **Security Group**.
3. Tambahkan Inbound Rule:
   - **Port Range**: `8000` (dan `80`, `443`, `22`)
   - **Protocol**: `TCP`
   - **Source**: `0.0.0.0/0` (Anywhere)

---

## 4. Metode 1: Deployment via Docker Compose (Direkomendasikan)

### Langkah 1: Install Docker & Docker Compose
```bash
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
```

### Langkah 2: Clone Repository
```bash
cd ~
git clone https://github.com/USERNAME_ANDA/pingon.git
cd pingon
```

### Langkah 3: Konfigurasi File `.env`
Salin file template `.env.example`:
```bash
cp .env.example .env
nano .env
```
Sesuaikan nilainya:
```env
PINGON_PORT=8000
JWT_SECRET_KEY=ganti_dengan_random_string_acak_dan_panjang_minimal_32_karakter!
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=1440
TZ=Asia/Jakarta
BACKUP_ENABLED=true
BACKUP_INTERVAL_HOURS=24
BACKUP_KEEP_COUNT=14
```
*Simpan dengan `CTRL + O`, lalu `Enter`, lalu `CTRL + X`.*

### Langkah 4: Build & Jalankan Container
```bash
docker compose up -d --build
```

### Langkah 5: Cek Status
```bash
docker compose ps
docker compose logs -f
```
Jika status menampilkan **`Up (healthy)`**, aplikasi sudah berjalan online!

---

## 5. Metode 2: Deployment Native via Python Systemd Service

Jika memilih menjalankan langsung di OS tanpa Docker:

### Langkah 1: Install Python & Dependensi Sistem
```bash
sudo apt install -y python3 python3-pip python3-venv iputils-ping git
```

### Langkah 2: Setup Direktori & Virtual Environment
```bash
sudo mkdir -p /opt/pingon
sudo chown -R $USER:$USER /opt/pingon
git clone https://github.com/USERNAME_ANDA/pingon.git /opt/pingon
cd /opt/pingon

python3 -m venv /opt/pingon/venv
source /opt/pingon/venv/bin/activate
pip install --upgrade pip
pip install -r backend/requirements.txt
```

### Langkah 3: Konfigurasi Environment Backend
```bash
cp backend/.env.example backend/.env
nano backend/.env
```

### Langkah 4: Buat Systemd Service
```bash
sudo nano /etc/systemd/system/pingon.service
```
Isi konfigurasi berikut:
```ini
[Unit]
Description=PingOn Network Monitoring Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/pingon/backend
ExecStart=/opt/pingon/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=5
EnvironmentFile=/opt/pingon/backend/.env

[Install]
WantedBy=multi-user.target
```

### Langkah 5: Aktifkan Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pingon
sudo systemctl status pingon
```

---

## 6. Konfigurasi Domain & HTTPS SSL (Nginx + Let's Encrypt)

Agar aplikasi dapat diakses dengan domain resmi ber-SSL HTTPS (contoh: `https://ping.domainanda.com`):

### 1. Install Nginx & Certbot
```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 2. Buat File Konfigurasi Nginx
```bash
sudo nano /etc/nginx/sites-available/pingon
```
Isi dengan:
```nginx
server {
    listen 80;
    server_name ping.domainanda.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }
}
```

### 3. Aktifkan Site & Generate SSL Gratis
```bash
sudo ln -s /etc/nginx/sites-available/pingon /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Pasang SSL Otomatis
sudo certbot --nginx -d ping.domainanda.com
```

---

## 7. Akses Pertama Kali & Pengaturan Notifikasi Telegram

### A. Login Pertama Kali
Buka di browser:
- `http://IP_SERVER:8000` atau `https://ping.domainanda.com`
- **Username Default**: `admin`
- **Password Default**: `admin123`

> ⚠️ **Sangat Disarankan**: Segera ganti password default setelah login melalui tombol **Change Password** di kiri bawah sidebar.

### B. Menghubungkan Telegram Alert
1. **Buat Bot Telegram**:
   - Chat ke [@BotFather](https://t.me/BotFather) di Telegram.
   - Ketik `/newbot`, ikuti petunjuk nama bot, dan Anda akan mendapatkan **Bot Token** (contoh: `123456789:ABCdefGhI...`).
2. **Dapatkan Chat ID Telegram**:
   - Chat ke [@userinfobot](https://t.me/userinfobot) di Telegram untuk melihat **Id** akun Anda.
   - Atau jika ingin notifikasi ke grup, tambahkan bot Anda ke grup tersebut lalu gunakan [@RawDataBot](https://t.me/RawDataBot) untuk melihat `chat_id` grup (biasanya diawali tanda minus `-100xxxx`).
3. **Masukkan ke PingOn**:
   - Klik tombol **+ Add Target** pada dashboard.
   - Centang **Telegram Notification**.
   - Masukkan Bot Token & Chat ID.
   - Klik **Test Telegram** untuk memastikan notifikasi terkirim.
   - Klik **Add Target** untuk mulai memonitor secara real-time.

---

## 8. Perawatan & Update Aplikasi (Maintenance)

### A. Update Kode Aplikasi ke Versi Terbaru (Docker)
Jika ada pembaruan kode di GitHub:
```bash
cd ~/pingon
git pull origin main
docker compose build --no-cache
docker compose up -d
```

### B. Perintah Berguna Docker
```bash
# Cek log aplikasi secara live
docker compose logs -f

# Restart aplikasi
docker compose restart

# Stop aplikasi
docker compose down

# Lokasi volume database persisten
# Tersimpan aman di Docker volume 'pingon-data'
```

### C. Backup Database Manual
File database tersimpan di `/app/backend/data/ping_monitor.db`.
Untuk mengambil file backup dari container:
```bash
docker cp pingon:/app/backend/data/ping_monitor.db ~/backup_pingon_$(date +%F).db
```
