import os
import sys
import ftplib
import io

def env(name, default=""):
    return (os.environ.get(name) or default).strip()

def connect():
    host = env("FTP_HOST") or env("FTP_SERVER")
    user = env("FTP_USER") or env("FTP_USERNAME")
    pw = env("FTP_PASSWORD")
    
    if not (host and user and pw):
        sys.exit("Chybí FTP přihlašovací údaje v proměnných prostředí (Secrets).")
        
    tls = env("FTP_TLS") == "1"
    ftp = ftplib.FTP_TLS(timeout=60) if tls else ftplib.FTP(timeout=60)
    ftp.connect(host, int(env("FTP_PORT", "21")))
    ftp.login(user, pw)
    if tls:
        ftp.prot_p()
    ftp.set_pasv(True)
    ftp.voidcmd("TYPE I")
    return ftp

def goto_dir(ftp, path):
    if path.startswith("/"):
        ftp.cwd("/")
    for part in [p for p in path.split("/") if p]:
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            ftp.mkd(part)
            ftp.cwd(part)

def upload_file():
    target = env("FTP_DIR", "/pipeline/")
    print(f"🔌 Připojuji se k FTP serveru...")
    ftp = connect()
    
    goto_dir(ftp, target)
    
    filename = "data.json"
    if not os.path.exists(filename):
        sys.exit(f"Lokální soubor {filename} neexistuje!")
        
    with open(filename, "rb") as f:
        data_bytes = f.read()
        
    tmp = filename + ".part"
    print(f"📤 Nahrávám {filename} přes .part...")
    ftp.storbinary(f"STOR {tmp}", io.BytesIO(data_bytes))
    
    # Přejmenování na finální název
    try:
        ftp.delete(filename)
    except Exception:
        pass
    ftp.rename(tmp, filename)
    
    print(f"✅ Soubor {filename} úspěšně nahrán do {target}!")
    ftp.quit()

if __name__ == "__main__":
    upload_file()
  
