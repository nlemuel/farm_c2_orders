import json
import re
import unicodedata
from urllib.parse import urlsplit, urljoin, urlunsplit, parse_qsl, urlencode
from datetime import datetime, timedelta
from .filters import window
from .auth import ensure_session, EXPIRED
from .utils import AuthError, ConfigError, DataError

class ListingChanged(DataError):
    """A consulta mudou durante a paginação e pode ser repetida integralmente."""


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


def collect(context, page, config, adapter, logger, period=None):
    if adapter.get('verified') is not True:
        raise ConfigError('Coletor ainda não validado no ADM real. Siga a calibração no README.')
    mode = adapter.get('mode')
    if mode == 'api':
        return collect_api(context, page, config, adapter, logger)
    if mode == 'dom':
        try:
            return collect_dom(page, config, adapter, logger, period=period)
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


def collect_listing(page, config, adapter, logger, status):
    spec = adapter['dom']
    required = ('table_selector', 'ready_selector', 'empty_selector')
    if any(not spec.get(k) for k in required) or any(not spec.get('columns', {}).get(k) for k in FIELDS if k != 'order_id'):
        raise ConfigError('Seletores/cabeçalhos DOM incompletos.')
    if not spec.get('single_page') and not (spec.get('next_selector') and spec.get('last_page_selector')):
        raise ConfigError('Configure controles de próxima/última página ou single_page verificado.')
    result, seen_pages, seen_ids = [], set(), set()
    expected_total = None
    listing_url = page.url
    for index in range(int(adapter.get('max_pages', 500))):
        ensure_session(page, config, adapter)
        page.locator(spec['ready_selector']).wait_for(state='visible')
        counter = page.locator(spec.get('total_selector') or spec['ready_selector']).inner_text()
        match = re.fullmatch(r'Exibidos\s+([0-9.]+)\s+de\s+([0-9.]+)\s+resultados', ' '.join(counter.split()), re.I)
        if not match:
            raise DataError('Contador do ADM não reconhecido; nenhuma escrita iniciada.')
        shown, total = (int(value.replace('.', '')) for value in match.groups())
        if expected_total is None:
            expected_total = total
        if total != expected_total:
            logger.warning('listagem_alterada status=%s pagina=%d total_inicial=%d total_atual=%d', status, index + 1, expected_total, total)
            raise ListingChanged(f'{status}: total mudou de {expected_total} para {total} na página {index + 1}.')
        if page.locator(spec['empty_selector']).is_visible():
            if index or total != 0 or shown != 0:
                raise DataError('Página vazia inesperada na paginação.')
            print(f'Coleta {status}: 0 de 0 ordens.')
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
        if any(' '.join(str(row['status']).split()).upper() != status for row in rows):
            raise DataError(f'A aba {status} retornou outro status; nenhuma escrita iniciada.')
        ids = [str(row['order_id']) for row in rows]
        if len(set(ids)) != len(ids) or seen_ids.intersection(ids):
            raise ListingChanged(f'{status}: ordens repetidas entre páginas; consulta instável.')
        seen_ids.update(ids)
        fingerprint = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        if fingerprint in seen_pages:
            raise DataError('Página repetida; lote abortado.')
        seen_pages.add(fingerprint)
        result.extend(rows)
        logger.info('status=%s pagina=%d ordens=%d acumulado=%d total_adm=%d', status, index + 1, len(rows), len(result), total)
        if len(result) > total or shown > total:
            raise DataError('Contagem inconsistente no ADM; nenhuma escrita iniciada.')
        next_button = page.locator(spec['next_selector']) if spec.get('next_selector') else None
        has_next = next_button is not None and next_button.is_visible() and next_button.is_enabled()
        last_marker = bool(spec.get('last_page_selector')) and page.locator(spec['last_page_selector']).is_visible()
        # O ADM omite toda a paginação quando a consulta cabe em uma página.
        # Total conferido + ausência de próxima página também confirma o fim.
        if len(result) == total:
            if has_next:
                raise DataError(f'Paginação inconsistente de {status}: total atingido com próxima página ativa; nenhuma escrita iniciada.')
            ensure_session(page, config, adapter)
            print(f'Coleta {status}: {len(result)} de {total} ordens; {index + 1} página(s).')
            return result
        if spec.get('single_page') or last_marker or not has_next:
            raise DataError(f'Coleta incompleta de {status}: {len(result)} de {total}; próxima página indisponível ou fim antecipado. Nenhuma escrita iniciada.')
        href = next_button.get_attribute('href')
        target = checked_next_url(listing_url, page.url, href)
        page.goto(target, wait_until='domcontentloaded')
        page.locator(adapter['authenticated_selector']).wait_for(state='visible')
    raise DataError('Limite de páginas atingido; nenhuma escrita iniciada.')


STATUS_TABS = (('AGUARDANDO', 'Aguardando'), ('EM PROCESSO', 'Em processo'))


def collect_dom_once(page, config, adapter, logger, period):
    """Abre ambas as abas a partir dos links reais, sem inferir parâmetros do ADM."""
    base = urlsplit(config.adm_url)
    clean_url = urlunsplit((base.scheme, base.netloc, base.path, '', ''))
    page.goto(clean_url, wait_until='domcontentloaded')
    page.locator(adapter['authenticated_selector']).wait_for(state='visible')
    ensure_session(page, config, adapter)
    date_names = ('another_find[data_init]', 'another_find[data_end]')
    for name in date_names:
        if page.locator(f'input[type="date"][name="{name}"]').count() != 1:
            raise ConfigError('Filtro de datas do ADM mudou; nenhuma escrita iniciada.')
    targets = []
    for status, label in STATUS_TABS:
        link = page.get_by_role('link', name=label, exact=True)
        if link.count() != 1:
            raise ConfigError(f'Link único da aba {label} não encontrado no ADM.')
        href = link.get_attribute('href')
        if not href:
            raise ConfigError(f'Aba {label} sem endereço de listagem.')
        target = urljoin(clean_url, href)
        parts = urlsplit(target)
        if (parts.scheme, parts.netloc, parts.path.rstrip('/')) != (base.scheme, base.netloc, base.path.rstrip('/')) or parts.username or parts.password:
            raise ConfigError(f'Endereço inesperado na aba {label}.')
        targets.append((status, dated_listing_url(target, period)))
    if targets[0][1] == targets[1][1]:
        raise ConfigError('As duas abas apontam para a mesma listagem.')
    result, ids = [], set()
    for status, target in targets:
        page.goto(target, wait_until='domcontentloaded')
        page.locator(adapter['authenticated_selector']).wait_for(state='visible')
        rows = collect_listing(page, config, adapter, logger, status)
        current = {str(row['order_id']) for row in rows}
        if ids.intersection(current):
            raise ListingChanged('Uma ordem mudou de status entre as duas abas.')
        ids.update(current)
        result.extend(rows)
        logger.info('coleta_concluida status=%s ordens=%d', status, len(rows))
    return result


def dated_listing_url(url, period):
    # Nomes observados no formulário GET. O limite final do ADM é exclusivo.
    parts = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if k not in ('page', 'another_find[data_init]', 'another_find[data_end]')]
    params.extend((('another_find[data_init]', period[0].date().isoformat()),
                   ('another_find[data_end]', (period[1].date() + timedelta(days=1)).isoformat())))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ''))


def checked_next_url(first_url, current_url, href):
    if not href:
        raise DataError('Próxima página sem endereço; nenhuma escrita iniciada.')
    first, current = urlsplit(first_url), urlsplit(current_url)
    target = urljoin(current_url, href)
    nxt = urlsplit(target)
    if (nxt.scheme, nxt.netloc, nxt.path.rstrip('/')) != (first.scheme, first.netloc, first.path.rstrip('/')) or nxt.username or nxt.password:
        raise DataError('Próxima página fora da listagem esperada.')
    def filters(parts):
        return sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != 'page')
    if filters(first) != filters(nxt):
        raise DataError('A paginação perdeu ou alterou filtros de status/data; nenhuma escrita iniciada.')
    def number(parts):
        pages = [v for k, v in parse_qsl(parts.query) if k == 'page']
        if not pages:
            return 1
        if len(pages) != 1 or not pages[0].isdigit():
            raise DataError('Número de página inválido.')
        return int(pages[0])
    if number(nxt) != number(current) + 1:
        raise DataError('Próxima página não é consecutiva; nenhuma escrita iniciada.')
    return target


def collect_dom(page, config, adapter, logger, period=None):
    period = period or window(datetime.now(config.tz))
    logger.info('consulta_adm data_inicial=%s data_final_exclusiva=%s',
                period[0].date(), period[1].date() + timedelta(days=1))
    for attempt in range(1, 4):
        try:
            return collect_dom_once(page, config, adapter, logger, period)
        except ListingChanged as exc:
            logger.warning('consulta_instavel tentativa=%d detalhe=%s', attempt, exc)
            if attempt == 3:
                raise DataError(f'ADM continuou mudando após 3 tentativas: {exc} Nenhuma escrita iniciada.') from None
            print(f'ADM mudou durante a leitura ({exc}); reiniciando ambas as abas, tentativa {attempt + 1}/3.')
