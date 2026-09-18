import json
import unicodedata
from urllib.parse import urlsplit
from .auth import ensure_session, EXPIRED
from .utils import AuthError, ConfigError, DataError

FIELDS = ('order_id', 'codent', 'email', 'valor', 'data_hora', 'status')


def header_positions(headers, columns):
    """Ignora diferenças de apresentação, mas rejeita correspondências ambíguas."""
    def normalize(value):
        return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()

    normalized = [normalize(h) for h in headers]
    positions = {}
    for key, name in columns.items():
        matches = [i for i, value in enumerate(normalized) if value == normalize(name)]
        if len(matches) != 1:
            # Apenas cabeçalhos: nunca imprime linhas, cookies ou tokens.
            labels = [' '.join(h.split())[:100] for h in headers[:50]]
            raise DataError(
                f'Cabeçalho do campo {key!r}: esperado {name!r}; '
                f'encontrado {len(matches)} vez(es). Cabeçalhos disponíveis: '
                + json.dumps(labels, ensure_ascii=False)
            )
        positions[key] = matches[0]
    if len(set(positions.values())) != len(positions):
        raise DataError('Dois campos foram associados à mesma coluna; revise columns.')
    return positions


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


# Atributo e link observados no HTML fornecido pelo usuário.
ROW_READER = r"""(rows, positions) => rows.map(row => {
    const cells = row.querySelectorAll(':scope > td');
    const record = Object.fromEntries(Object.entries(positions).map(([key, i]) =>
        [key, cells[i] ? cells[i].innerText.trim() : null]));
    const ids = [];
    const match = (row.getAttribute('id') || '').match(/^item_([0-9]+)$/);
    if (match) ids.push(match[1]);
    for (const link of row.querySelectorAll('a[href]')) {
        const url = new URL(link.getAttribute('href'), document.baseURI);
        const id = url.pathname.match(/^\/adm-payment\/([0-9]+)\/?$/);
        if (url.origin === location.origin && id) ids.push(id[1]);
    }
    record._order_ids = ids;
    return record;
})"""


def resolve_order_ids(rows):
    result = []
    for raw in rows:
        row = dict(raw)
        candidates = row.pop('_order_ids', [])
        if row.get('order_id'):
            candidates = [str(row['order_id']).strip(), *candidates]
        unique = set(candidates)
        if len(unique) != 1:
            raise DataError('ORDER_ID ausente ou divergente entre coluna, atributo da linha e link Visualizar. Nenhuma escrita iniciada.')
        row['order_id'] = unique.pop()
        result.append(row)
    return result


def dom_positions(headers, columns):
    columns = dict(columns)
    id_header = columns.pop('order_id', None) or 'Id'
    normalize = lambda value: ' '.join(unicodedata.normalize('NFC', value).split()).casefold()
    # Ausência da coluna é permitida; ambiguidade continua sendo erro.
    if any(normalize(h) == normalize(id_header) for h in headers):
        columns['order_id'] = id_header
    return header_positions(headers, columns)


def collect_dom(page, config, adapter, logger):
    spec = adapter['dom']
    required = ('table_selector', 'ready_selector', 'empty_selector')
    if any(not spec.get(k) for k in required) or any(not spec.get('columns', {}).get(k) for k in FIELDS if k != 'order_id'):
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
        table.wait_for(state='visible')
        table.locator('thead th').first.wait_for(state='attached')
        headers = table.locator('thead th').all_text_contents()
        positions = dom_positions(headers, spec['columns'])
        raw_rows = table.locator('tbody tr').evaluate_all(ROW_READER, positions)
        rows = resolve_order_ids(raw_rows)
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
        wait_js = """({selector, positions, oldRows}) => {
          const table = document.querySelector(selector);
          if (!table) return false;
          const readRows = """ + ROW_READER + """;
          const rows = readRows(Array.from(table.querySelectorAll('tbody tr')), positions);
          return rows.length > 0 && JSON.stringify(rows) !== JSON.stringify(oldRows);
        }"""
        page.wait_for_function(wait_js, arg={
            'selector': spec['table_selector'], 'positions': positions, 'oldRows': raw_rows})
    raise DataError('Limite de páginas atingido; nenhuma escrita iniciada.')
