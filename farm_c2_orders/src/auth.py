import re
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
from .utils import AuthError, ConfigError, atomic_secret

MISSING = 'ERRO DE AUTENTICAÇÃO\nNenhuma sessão do ADM foi encontrada.\nExecute: python main.py --login'
EXPIRED = 'SESSÃO ADM EXPIRADA\nA sessão salva não é mais válida.\nExecute: python main.py --login\nNenhuma alteração foi realizada.'


def authenticated(page, config, adapter):
    current, expected = urlsplit(page.url), urlsplit(config.adm_url)
    return (current.scheme == expected.scheme and current.netloc == expected.netloc
            and current.path.rstrip('/') == expected.path.rstrip('/')
            and page.locator(adapter['authenticated_selector']).is_visible())


def ensure_session(page, config, adapter):
    if re.search(adapter.get('login_url_pattern', r'/login(?:[/?#]|$)'), page.url):
        raise AuthError(EXPIRED)
    if not authenticated(page, config, adapter):
        raise ConfigError('Não foi possível confirmar a área autenticada. Verifique o seletor e o ADM; nenhuma escrita iniciada.')


@contextmanager
def session(config, login=False):
    if not login and not config.state.is_file():
        raise AuthError(MISSING)
    adapter = config.adapter()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not login, channel='chromium')
        try:
            try:
                context = browser.new_context(**({} if login else {'storage_state': str(config.state)}))
            except Exception:
                raise AuthError('Não foi possível carregar a sessão. Execute: python main.py --login') from None
            page = context.new_page()
            page.set_default_timeout(30000)
            response = page.goto(config.adm_url, wait_until='domcontentloaded')
            if not login and response and response.status in (401, 403):
                raise AuthError(EXPIRED)
            if login:
                print('Faça login manualmente no navegador oficial. Após entrar, abra a página de pagamentos.')
                deadline = time.monotonic() + int(config.get('LOGIN_TIMEOUT_SECONDS', '300'))
                while time.monotonic() < deadline:
                    if page.is_closed():
                        raise AuthError('Login cancelado. A sessão anterior foi preservada.')
                    if authenticated(page, config, adapter):
                        break
                    page.wait_for_timeout(500)
                else:
                    raise AuthError('Tempo de login esgotado. A sessão anterior foi preservada.')
            else:
                # Aguarda renderização sem interpretar falha de rede como expiração.
                try:
                    page.locator(adapter['authenticated_selector']).wait_for(state='visible', timeout=15000)
                except Exception:
                    pass
            ensure_session(page, config, adapter)
            if login:
                atomic_secret(config.state, context.storage_state(indexed_db=True))
            yield context, page, adapter
        finally:
            browser.close()
