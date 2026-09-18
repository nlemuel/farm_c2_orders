import json
from urllib.parse import urlsplit
from .auth import ensure_session, EXPIRED
from .utils import AuthError, ConfigError, DataError

FIELDS = ('order_id', 'codent', 'email', 'valor', 'data_hora', 'status')


def at(value, path):
    for key in path.split('.') if path else []:
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def collect(context, page, config, adapter, logger):
    if adapter.get('verified') is not True:
        raise ConfigError('Coletor ainda não validado no ADM real. Siga a calibração no README.')
    mode = adapter.get('mode')
    if mode == 'api':
        return collect_api(context, page, config, adapter, logger)
    if mode == 'dom':
        try:
            return collect_dom(page, config, adapter, logger)
        except Exception:
            # Um redirecionamento durante a paginação deve ser classificado como
            # sessão expirada, inclusive quando primeiro causou timeout do DOM.
            ensure_session(page, config, adapter)
            raise
    raise ConfigError('Modo do coletor deve ser api ou dom.')


def collect_api(context, page, config, adapter, logger):
    spec = adapter['api']
    url = urlsplit(spec['url'])
    base = urlsplit(config.adm_url)
    if url.scheme != 'https' or url.netloc != base.netloc or url.username or url.password or url.query or url.fragment:
        raise ConfigError('API deve usar HTTPS, mesmo host do ADM e URL sem credenciais/query.')
    if not spec.get('page_param') or not spec.get('total_pages_path') or any(not spec['fields'].get(k) for k in FIELDS):
        raise ConfigError('Mapeamento/paginação da API incompleto.')
    result, fingerprints = [], set()
    first = int(spec.get('first_page', 1))
    expected_total = None
    for index in range(int(adapter.get('max_pages', 500))):
        ensure_session(page, config, adapter)
        params = dict(spec.get('params', {}))
        params[spec['page_param']] = first + index
        # Somente GET observado e autorizado; cookies vêm do mesmo BrowserContext.
        response = context.request.get(spec['url'], params=params, max_redirects=0, timeout=30000)
        try:
            if response.status in (401, 403):
                raise AuthError(EXPIRED)
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get('location', '')
                if '/login' in location:
                    raise AuthError(EXPIRED)
                raise DataError('API redirecionou; coleta abortada.')
            if response.status != 200 or 'json' not in response.headers.get('content-type', ''):
                raise DataError('Resposta inesperada da API; coleta abortada.')
            try:
                body = response.json()
                items = at(body, spec.get('items_path', ''))
                total = at(body, spec['total_pages_path'])
                if isinstance(total, bool) or not isinstance(total, int) or total < 0 or not isinstance(items, list):
                    raise ValueError()
                rows = [{key: at(item, spec['fields'][key]) for key in FIELDS} for item in items]
            except (ValueError, KeyError, TypeError, IndexError):
                raise DataError('Formato da API mudou; lote abortado.') from None
        finally:
            response.dispose()
        if expected_total is None:
            expected_total = total
        if total != expected_total or (not rows and index < total - 1) or (total == 0 and rows):
            raise DataError('Paginação instável/incompleta; lote abortado.')
        fingerprint = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        if rows and fingerprint in fingerprints:
            raise DataError('Página repetida; lote abortado.')
        fingerprints.add(fingerprint)
        result.extend(rows)
        logger.info('pagina=%d ordens=%d', index + 1, len(rows))
        if index + 1 >= total:
            return result
    raise DataError('Limite de páginas atingido; nenhuma escrita iniciada.')


def collect_dom(page, config, adapter, logger):
    spec = adapter['dom']
    required = ('table_selector', 'ready_selector', 'empty_selector')
    if any(not spec.get(k) for k in required) or any(not spec.get('columns', {}).get(k) for k in FIELDS):
        raise ConfigError('Seletores/cabeçalhos DOM incompletos.')
    if not spec.get('single_page') and not (spec.get('next_selector') and spec.get('last_page_selector')):
        raise ConfigError('Configure controles de próxima/última página ou single_page verificado.')
    result, seen_pages = [], set()
    for index in range(int(adapter.get('max_pages', 500))):
        ensure_session(page, config, adapter)
        page.locator(spec['ready_selector']).wait_for(state='visible')
        if page.locator(spec['empty_selector']).is_visible():
            if index:
                raise DataError('Página vazia inesperada na paginação.')
            return []
        table = page.locator(spec['table_selector'])
        headers = [h.strip() for h in table.locator('thead th').all_text_contents()]
        positions = {}
        for key, name in spec['columns'].items():
            if headers.count(name) != 1:
                raise DataError('Cabeçalhos ausentes/ambíguos no ADM.')
            positions[key] = headers.index(name)
        # Lê apenas as seis células necessárias; não captura a página inteira.
        rows = table.locator('tbody tr').evaluate_all('''(rows, positions) => rows.map(row => {
            const cells = row.querySelectorAll('td');
            return Object.fromEntries(Object.entries(positions).map(([key, i]) =>
                [key, cells[i] ? cells[i].innerText.trim() : null]));
        })''', positions)
        if not rows or any(v is None for row in rows for v in row.values()):
            raise DataError('Tabela vazia/irregular sem indicador de vazio confirmado.')
        fingerprint = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        if fingerprint in seen_pages:
            raise DataError('Página repetida; lote abortado.')
        seen_pages.add(fingerprint)
        result.extend(rows)
        logger.info('pagina=%d ordens=%d', index + 1, len(rows))
        if spec.get('single_page') or page.locator(spec['last_page_selector']).is_visible():
            ensure_session(page, config, adapter)
            return result
        next_button = page.locator(spec['next_selector'])
        if not next_button.is_visible() or not next_button.is_enabled():
            raise DataError('Fim da paginação não confirmado; lote abortado.')
        next_button.click()
        # Aguarda troca das células, não um sleep fixo que poderia reler a página anterior.
        page.wait_for_function('''({selector, positions, oldRows}) => {
          const table = document.querySelector(selector);
          if (!table) return false;
          const rows = Array.from(table.querySelectorAll('tbody tr')).map(row => {
            const cells = row.querySelectorAll('td');
            return Object.fromEntries(Object.entries(positions).map(([key,i]) =>
              [key, cells[i] ? cells[i].innerText.trim() : null]));
          });
          return rows.length > 0 && JSON.stringify(rows) !== JSON.stringify(oldRows);
        }''', arg={'selector': spec['table_selector'], 'positions': positions, 'oldRows': rows})
    raise DataError('Limite de páginas atingido; nenhuma escrita iniciada.')
