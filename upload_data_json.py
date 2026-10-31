import os
import sys
import ftplib
import io

def env(name, default=""):
    return (os.environ.get(name) or default).strip()

def connect():
    # Používáme přesně vaše názvy z GitHub Secrets
    host = env("FTP_SERVER")
    user = env("FTP_USERNAME")
    pw = env("FTP_PASSWORD")
    
    if not (host and user and pw):
        sys.exit("Chybí FTP_SERVER, FTP_USERNAME nebo FTP_PASSWORD v GitHub Secrets.")
        
    ftp = ftplib.FTP(timeout=60)
    ftp.connect(host, 21)
    ftp.login(user, pw)
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
    target = "pipeline"  # Cílová složka na Webzdarma
    print(f"🔌 Připojuji se k FTP serveru ({env('FTP_SERVER')})...")
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
    
    try:
        ftp.delete(filename)
    except Exception:
        pass
    ftp.rename(tmp, filename)
    
    print(f"✅ Soubor {filename} úspěšně nahrán do složky {target}!")
    ftp.quit()

if __name__ == "__main__":
    upload_file()
