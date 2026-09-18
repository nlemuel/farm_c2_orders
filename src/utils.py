import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


class AppError(Exception):
    code = 1


class AuthError(AppError):
    code = 2


class ConfigError(AppError):
    code = 3


class DataError(AppError):
    code = 4


class WriteUncertain(AppError):
    code = 5


class BusyError(AppError):
    code = 6


@dataclass
class Config:
    root: Path = ROOT

    def get(self, key, default=''):
        return os.environ.get(key, default)

    def path(self, key, default):
        return self.root / self.get(key, default)

    @property
    def tz(self):
        if self.get('TIMEZONE', 'America/Sao_Paulo') != 'America/Sao_Paulo':
            raise ConfigError('TIMEZONE deve ser America/Sao_Paulo.')
        return ZoneInfo('America/Sao_Paulo')

    @property
    def adm_url(self):
        return self.get('ADM_URL', 'https://adm.melhorescola.com.br/adm-payment')

    @property
    def state(self):
        return self.path('PLAYWRIGHT_STORAGE_STATE', 'auth/storage_state.json')

    def adapter(self):
        try:
            result = json.loads(self.path('ADM_ADAPTER_PATH', 'adm_adapter.json').read_text('utf-8'))
            if not result.get('authenticated_selector'):
                raise ValueError()
            return result
        except (OSError, ValueError, TypeError):
            raise ConfigError('Configure adm_adapter.json com um seletor exclusivo da área autenticada.') from None


def atomic_secret(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.session-', dir=path.parent)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def identifier(value, *, field='identificador', location='origem não informada'):
    def invalid(reason):
        raise DataError(f'{field} inválido em {location}: {reason}. Nenhuma escrita iniciada.')
    if value is None:
        invalid('valor ausente')
    if isinstance(value, bool):
        invalid('valor booleano em vez de identificador')
    text = str(value).strip()
    if re.fullmatch(r'\d+\.0+', text):
        text = text.split('.')[0]
    if not text:
        invalid('valor vazio')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', text):
        invalid('formato não aceito; contém caracteres fora de letras, números, hífen e sublinhado')
    return text


def money(value):
    text = str(value).strip().replace('R$', '').replace('\xa0', '').replace(' ', '')
    if ',' in text:
        if not re.fullmatch(r'-?(?:\d+|\d{1,3}(?:\.\d{3})+),\d{2}', text):
            raise DataError('Valor monetário inválido.')
        text = text.replace('.', '').replace(',', '.')
    try:
        number = Decimal(text)
        if not number.is_finite() or number < 0 or number != number.quantize(Decimal('.01')):
            raise ValueError()
        return number
    except (InvalidOperation, ValueError):
        raise DataError('Valor monetário inválido.') from None


def timestamp(value, tz):
    text = str(value).strip()
    try:
        result = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        result = None
        for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M'):
            try:
                result = datetime.strptime(text, fmt)
                break
            except ValueError:
                pass
        if result is None:
            raise DataError('Data/hora inválida; somente data não é suficiente.')
    if ':' not in text:
        raise DataError('Data da ordem precisa incluir horário.')
    return result.replace(tzinfo=tz) if result.tzinfo is None else result.astimezone(tz)


def mask_email(email):
    local, _, domain = email.partition('@')
    return local[:2] + '***@' + domain


def setup_log(config):
    directory = config.root / 'logs'
    directory.mkdir(exist_ok=True)
    logger = logging.getLogger('farm_c2')
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(directory / (datetime.now(config.tz).date().isoformat() + '.log'), encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    return logger


@contextmanager
def execution_lock(root):
    """Lock do SO; libera automaticamente mesmo se o processo morrer."""
    (root / 'logs').mkdir(exist_ok=True)
    with (root / 'logs' / 'execution.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BusyError('Outra execução deste projeto está ativa.') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
