"""Configuração Google opcional; não faz parte do login ADM."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from src.utils import Config, AppError
from src.sheets_client import google_credentials

if __name__ == '__main__':
    config = Config()
    load_dotenv(config.root / '.env')
    try:
        google_credentials(config, interactive=True)
        print('Credencial Google validada.')
    except Exception as exc:
        print(str(exc) if isinstance(exc, AppError) else 'Falha no OAuth Google. Verifique a configuração.')
        raise SystemExit(3)
