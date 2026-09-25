import json
from datetime import datetime
from urllib.parse import quote
from .utils import ConfigError, DataError, WriteUncertain, atomic_secret, identifier

DEFAULT_CHECK = 'NÃO CHAMEI AINDA'

SCOPE = 'https://www.googleapis.com/auth/spreadsheets'
LOG_HEADERS = ['ORDER_ID', 'DATA_ORDEM', 'CODENT', 'EMAIL', 'VALOR', 'STATUS_ORIGEM', 'EXECUTADO_EM', 'MODO']
DEST_HEADERS = ['DATA DA ORDEM', 'CODENT', 'E-MAIL', 'VALOR']


def google_credentials(config, interactive=False):
    mode = config.get('GOOGLE_AUTH_MODE', 'service_account')
    if mode == 'service_account':
        from google.oauth2.service_account import Credentials
        path = config.path('GOOGLE_CREDENTIALS_PATH', 'credentials/google-service-account.json')
        if not path.is_file():
            raise ConfigError('Configure a credencial da Service Account; consulte o README.')
        return Credentials.from_service_account_file(str(path), scopes=[SCOPE])
    if mode != 'oauth':
        raise ConfigError('GOOGLE_AUTH_MODE deve ser service_account ou oauth.')
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    token = config.path('GOOGLE_OAUTH_TOKEN_PATH', 'auth/google-token.json')
    credentials = Credentials.from_authorized_user_file(str(token), [SCOPE]) if token.exists() else None
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        atomic_secret(token, json.loads(credentials.to_json()))
    if not credentials or not credentials.valid:
        if not interactive:
            raise ConfigError('OAuth Google ausente/expirado. Execute: python scripts/google_oauth_login.py')
        from google_auth_oauthlib.flow import InstalledAppFlow
        credentials = InstalledAppFlow.from_client_secrets_file(
            str(config.path('GOOGLE_OAUTH_CLIENT_PATH', 'credentials/google-oauth-client.json')), [SCOPE]
        ).run_local_server(port=0)
        atomic_secret(token, json.loads(credentials.to_json()))
    return credentials


def cell(value):
    # stringValue impede interpretação como fórmula de e-mails/identificadores.
    return {'userEnteredValue': {'numberValue' if isinstance(value, (int, float)) else 'stringValue': value}}


def update(sheet_id, start, rows):
    return {'updateCells': {'start': {'sheetId': sheet_id, 'rowIndex': start, 'columnIndex': 0},
                            'rows': [{'values': [cell(v) for v in row]} for row in rows],
                            'fields': 'userEnteredValue'}}


class SheetsClient:
    def __init__(self, config, transport=None):
        self.config = config
        if transport is None:
            from google.auth.transport.requests import AuthorizedSession
            transport = AuthorizedSession(google_credentials(config))
        self.http = transport
        self.source = config.get('CARTEIRA_SPREADSHEET_ID', '11XiHk1jB2R8p7EsptD4lZvFNmDX6nv0KDJM--MQn6Do')
        self.destination = config.get('DESTINO_SPREADSHEET_ID', '1OM0wnMFNN4zysciJPxh1iVgUUT9kCSLzw4h_g1Cf6wg')
        self.dest_name = config.get('DESTINO_SHEET', 'NICOLAS')
        self.log_name = config.get('LOG_SHEET', 'LOG_AUTOMACAO')
        self.routes = {'Farm C2': self.dest_name,
                       'Farm C1': config.get('DESTINO_SHEET_C1', 'MARIA')}
        names = [*self.routes.values(), self.log_name]
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ConfigError('Abas de Farm C2, Farm C1 e log devem ser preenchidas e diferentes.')

    def request(self, method, spreadsheet, suffix='', **kwargs):
        url = f'https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet, safe="")}{suffix}'
        try:
            response = self.http.request(method, url, timeout=60, **kwargs)
        except Exception:
            if method == 'POST':
                raise WriteUncertain('Resposta da escrita não confirmada. Não repita cegamente; execute --test para reler ORDER_IDs.') from None
            raise ConfigError('Falha de conexão com Google Sheets; nenhuma escrita iniciada.') from None
        if response.status_code >= 400:
            if method == 'POST' and response.status_code >= 500:
                raise WriteUncertain('Resposta incerta do Sheets. Execute --test antes de tentar novamente.')
            raise ConfigError(f'Google Sheets retornou HTTP {response.status_code}. Verifique permissões/configuração.')
        try:
            return response.json()
        except ValueError:
            if method == 'POST':
                raise WriteUncertain('Resposta da escrita inválida. Confira LOG_AUTOMACAO antes de repetir.') from None
            raise DataError('Resposta inválida do Google Sheets.') from None

    def metadata(self):
        payload = self.request('GET', self.destination, params={'fields': 'sheets.properties'})
        return {s['properties']['title']: s['properties'] for s in payload.get('sheets', [])}

    def values(self, spreadsheet, title, columns, render='FORMULA'):
        a1 = "'" + title.replace("'", "''") + "'!" + columns
        return self.request('GET', spreadsheet, '/values/' + quote(a1, safe=''),
                            params={'valueRenderOption': render}).get('values', [])

    def portfolios(self):
        title = self.config.get('CARTEIRA_SHEET', 'Escolas')
        rows = self.values(self.source, title, 'A1:ZZ1', render='UNFORMATTED_VALUE')
        if not rows:
            raise DataError('Aba Escolas vazia.')
        headers = [str(x).strip() for x in rows[0]]
        if headers.count('Codent') != 1 or headers.count('Carteira Revenue') != 1:
            raise DataError('Cabeçalhos de carteira ausentes/ambíguos.')
        ci, pi = headers.index('Codent'), headers.index('Carteira Revenue')
        def column(index):
            result = ''
            while index >= 0:
                index, remainder = divmod(index, 26)
                result = chr(65 + remainder) + result
                index -= 1
            return result
        codes = self.values(self.source, title, f'{column(ci)}2:{column(ci)}', render='UNFORMATTED_VALUE')
        portfolios = self.values(self.source, title, f'{column(pi)}2:{column(pi)}', render='UNFORMATTED_VALUE')
        mapping = {}
        for index, row in enumerate(codes):
            if not row or not str(row[0]).strip():
                continue
            codent = identifier(row[0], field='CODENT', location=f'carteiras, aba {title!r}, célula {column(ci)}{index + 2}')
            portfolio = str(portfolios[index][0]).strip() if index < len(portfolios) and portfolios[index] else ''
            if codent in mapping and mapping[codent] != portfolio:
                raise DataError('Codent associado a carteiras divergentes.')
            mapping[codent] = portfolio
        return mapping

    def snapshot(self):
        meta = self.metadata()
        if self.dest_name not in meta:
            raise DataError(f'Aba de destino {self.dest_name!r} não existe.')
        dest = self.values(self.destination, self.dest_name, 'A:D')
        if not dest or dest[0] != DEST_HEADERS:
            raise DataError(f'Cabeçalhos A:D de {self.dest_name!r} não correspondem ao esperado.')
        log = self.values(self.destination, self.log_name, 'A:H') if self.log_name in meta else []
        if log and log[0] != LOG_HEADERS:
            raise DataError('Cabeçalhos de LOG_AUTOMACAO não correspondem ao esperado.')
        processed = set()
        for row_number, row in enumerate(log[1:], start=2):
            if not row or not any(str(v).strip() for v in row):
                continue
            if len(row) < 8 or not row[0]:
                raise DataError('Linha incompleta no LOG_AUTOMACAO; revise antes de continuar.')
            processed.add(identifier(row[0], field='ORDER_ID', location=f'log, aba {self.log_name!r}, célula A{row_number}'))
        return meta, dest, log, processed

    def processed(self):
        return self.snapshot()[3]

    def check_template(self, sheet_name=None):
        """E2 é o modelo nativo da lista suspensa; somente leitura."""
        sheet_name = sheet_name or self.dest_name
        title = "'" + sheet_name.replace("'", "''") + "'!E1:E2"
        body = self.request('GET', self.destination, params={
            'ranges': title,
            'fields': 'sheets.data.rowData.values(userEnteredValue,dataValidation)'})
        try:
            rows = body['sheets'][0]['data'][0]['rowData']
            header = rows[0]['values'][0]['userEnteredValue']['stringValue']
            rule = rows[1]['values'][0]['dataValidation']
            condition = rule['condition']
            options = [v.get('userEnteredValue') for v in condition.get('values', [])]
        except (KeyError, IndexError, TypeError):
            raise DataError(f'Na aba {sheet_name!r}, configure CHECK em E1 e a lista suspensa modelo em E2.') from None
        if header.strip().upper() != 'CHECK':
            raise DataError(f'O cabeçalho E1 da aba {sheet_name!r} deve ser CHECK.')
        if condition.get('type') != 'ONE_OF_LIST' or DEFAULT_CHECK not in options:
            raise DataError(f'A lista de {sheet_name!r}!E2 precisa conter exatamente NÃO CHAMEI AINDA.')

    def snapshot_for_targets(self, targets):
        """Valida os destinos solicitados e lê o log global, incluindo IDs antigos."""
        if set(targets) - set(self.routes.values()):
            raise ConfigError('Destino não configurado para Farm C1/C2.')
        meta, dest, log, processed = self.snapshot()
        destinations = {self.dest_name: dest}
        for title in targets:
            if title == self.dest_name:
                continue
            if title not in meta:
                raise DataError(f'Aba de destino {title!r} não existe.')
            rows = self.values(self.destination, title, 'A:D')
            if not rows or rows[0] != DEST_HEADERS:
                raise DataError(f'Cabeçalhos A:D de {title!r} não correspondem ao esperado: DATA DA ORDEM, CODENT, E-MAIL, VALOR.')
            destinations[title] = rows
        return meta, destinations, log, processed

    def ranking_requests(self, meta, destinations, log, fresh):
        from .lead_ranking import plan_sheet
        def column(index):
            result = ''
            while index:
                index, remainder = divmod(index - 1, 26)
                result = chr(65 + remainder) + result
            return result
        ranges = ["'" + title.replace("'", "''") + "'!A1:" + column(meta[title]['gridProperties']['columnCount']) + str(min(len(rows) + 1, meta[title]['gridProperties']['rowCount']))
                  for title, rows in destinations.items() if title in fresh]
        body = self.request('GET', self.destination, params={
            'ranges': ranges,
            'fields': 'sheets(properties,conditionalFormats,data(rowData(values(userEnteredValue,effectiveValue,dataValidation))))'})
        requests = []
        for sheet in body.get('sheets', []):
            title = sheet['properties']['title']
            if title not in fresh:
                continue
            count = len(destinations[title])
            rows = sheet.get('data', [{}])[0].get('rowData', [])
            if len(rows) < count or (len(rows) > count and any(c.get('userEnteredValue') for c in rows[count].get('values', [])[:4])):
                raise DataError('Linhas da planilha mudaram durante a leitura; repita --test.')
            planned, summary = plan_sheet(sheet, rows[:count], log, fresh[title],
                                          legacy_year=int(self.config.get('LEGACY_DATE_YEAR', '2026')))
            requests.extend(planned)
        if len([s for s in body.get('sheets', []) if s['properties']['title'] in fresh]) != len(fresh):
            raise DataError('Não foi possível conferir todas as abas para ranking.')
        return requests

    def commit(self, orders, executed_at):
        # Compatibilidade com chamadas antigas de apenas Farm C2.
        return self.commit_batches({self.dest_name: orders}, executed_at)[self.dest_name]

    def commit_batches(self, batches, executed_at):
        # Um snapshot e um batchUpdate para NICOLAS + MARIA + log.
        meta, destinations, log, processed = self.snapshot_for_targets(batches)
        fresh, seen = {}, set()
        for title, orders in batches.items():
            fresh[title] = []
            for order in orders:
                if order.order_id in seen:
                    raise DataError('ORDER_ID repetido entre lotes; nenhuma escrita iniciada.')
                seen.add(order.order_id)
                if order.order_id not in processed:
                    fresh[title].append(order)
        for title, orders in fresh.items():
            if orders:
                self.check_template(title)
        ranking = self.ranking_requests(meta, destinations, log, fresh)
        requests = []
        total = sum(len(orders) for orders in fresh.values())
        if self.log_name not in meta:
            new_id = max((s['sheetId'] for s in meta.values()), default=0) + 1
            log_meta = {'sheetId': new_id, 'gridProperties': {
                'rowCount': max(1000, total + 1), 'columnCount': 8}}
            requests.append({'addSheet': {'properties': dict(log_meta, title=self.log_name)}})
        else:
            log_meta = meta[self.log_name]
        if not log:
            requests.append(update(log_meta['sheetId'], 0, [LOG_HEADERS]))

        def ensure_rows(sheet, needed):
            capacity = sheet['gridProperties']['rowCount']
            if needed > capacity:
                requests.append({'appendDimension': {'sheetId': sheet['sheetId'],
                    'dimension': 'ROWS', 'length': needed - capacity}})

        ensure_rows(log_meta, max(1, len(log)) + total)
        log_rows = []
        for title, orders in fresh.items():
            if not orders:
                continue
            destination = meta[title]
            start = len(destinations[title])
            ensure_rows(destination, start + len(orders))
            source_range = {'sheetId': destination['sheetId'], 'startRowIndex': 1,
                            'endRowIndex': 2, 'startColumnIndex': 4, 'endColumnIndex': 5}
            target_range = {'sheetId': destination['sheetId'], 'startRowIndex': start,
                            'endRowIndex': start + len(orders),
                            'startColumnIndex': 4, 'endColumnIndex': 5}
            for paste_type in ('PASTE_FORMAT', 'PASTE_DATA_VALIDATION'):
                requests.append({'copyPaste': {'source': source_range,
                    'destination': target_range, 'pasteType': paste_type,
                    'pasteOrientation': 'NORMAL'}})
            requests.append(update(destination['sheetId'], start, [
                [o.data_hora.strftime('%d/%m/%Y'), o.codent, o.email,
                 float(o.valor), DEFAULT_CHECK] for o in orders]))
            log_rows.extend([
                [o.order_id, o.data_hora.isoformat(), o.codent, o.email, float(o.valor),
                 o.status, executed_at.isoformat(), 'RUN'] for o in orders])
        if log_rows:
            requests.append(update(log_meta['sheetId'], max(1, len(log)), log_rows))
        requests.extend(ranking)
        if requests:
            # Não faz retry cego: ambas as abas e o log são atômicos na mesma planilha.
            self.request('POST', self.destination, ':batchUpdate', json={'requests': requests})
        return {title: len(orders) for title, orders in fresh.items()}
