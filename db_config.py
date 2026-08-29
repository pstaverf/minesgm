import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Built-in lightweight .env parser if python-dotenv is not yet installed
    if os.path.exists(".env"):
        try:
            with open(".env", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "8943696232:AAG3rX23_WNM6OSfKLLkgDxK2yo8-HZ1O4k")
BOT_USERNAME = os.getenv("BOT_USERNAME", "mines2gmbot")

admins_raw = os.getenv("ADMINS", "6539341659,6025818386")
ADMINS = [int(x.strip()) for x in admins_raw.split(",") if x.strip().isdigit()]

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:IYyTGByRhzZzfrAXcASigduVdidIbhsv@altaria.proxy.rlwy.net:56137/railway")
REDIS_URL = os.getenv("REDIS_URL", "redis://default:aXDkONwaDWeimAyhNhOjTPcrSemHedzq@switchyard.proxy.rlwy.net:27809")

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://kleymorf.shop")
WEB_SERVER_PORT = int(os.getenv("WEB_SERVER_PORT", "13153"))
