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
        if self.dest_name == self.log_name:
            raise ConfigError('Abas de destino e log devem ser diferentes.')

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
            raise DataError('Aba de destino não existe.')
        dest = self.values(self.destination, self.dest_name, 'A:D')
        if not dest or dest[0] != DEST_HEADERS:
            raise DataError('Cabeçalhos A:D de NICOLAS não correspondem ao esperado.')
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

    def check_template(self):
        """E2 é o modelo nativo da lista suspensa; somente leitura."""
        title = "'" + self.dest_name.replace("'", "''") + "'!E1:E2"
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
            raise DataError('Configure o cabeçalho CHECK em E1 e a lista suspensa modelo em E2.') from None
        if header.strip().upper() != 'CHECK':
            raise DataError('O cabeçalho da coluna E deve ser CHECK.')
        if condition.get('type') != 'ONE_OF_LIST' or DEFAULT_CHECK not in options:
            raise DataError('A lista suspensa de E2 precisa conter exatamente a opção NÃO CHAMEI AINDA.')

    def commit(self, orders, executed_at):
        # Releitura imediatamente antes da escrita; evita usar um snapshot antigo.
        meta, dest, log, processed = self.snapshot()
        orders = [o for o in orders if o.order_id not in processed]
        if orders:
            self.check_template()
        requests = []
        destination = meta[self.dest_name]
        if self.log_name not in meta:
            new_id = max((s['sheetId'] for s in meta.values()), default=0) + 1
            log_meta = {'sheetId': new_id, 'gridProperties': {'rowCount': max(1000, len(orders)+1), 'columnCount': 8}}
            requests.append({'addSheet': {'properties': dict(log_meta, title=self.log_name)}})
        else:
            log_meta = meta[self.log_name]
        if not log:
            requests.append(update(log_meta['sheetId'], 0, [LOG_HEADERS]))
        for sheet, needed in ((destination, len(dest) + len(orders)),
                              (log_meta, max(1, len(log)) + len(orders))):
            capacity = sheet['gridProperties']['rowCount']
            if needed > capacity:
                requests.append({'appendDimension': {'sheetId': sheet['sheetId'], 'dimension': 'ROWS', 'length': needed - capacity}})
        if orders:
            # Apenas E das linhas novas; nunca copia valores antigos nem toca F.
            source_range = {'sheetId': destination['sheetId'], 'startRowIndex': 1,
                            'endRowIndex': 2, 'startColumnIndex': 4, 'endColumnIndex': 5}
            target_range = {'sheetId': destination['sheetId'], 'startRowIndex': len(dest),
                            'endRowIndex': len(dest) + len(orders),
                            'startColumnIndex': 4, 'endColumnIndex': 5}
            for paste_type in ('PASTE_FORMAT', 'PASTE_DATA_VALIDATION'):
                requests.append({'copyPaste': {'source': source_range,
                    'destination': target_range, 'pasteType': paste_type,
                    'pasteOrientation': 'NORMAL'}})
            requests.append(update(destination['sheetId'], len(dest), [
                [o.data_hora.strftime('%d/%m/%Y'), o.codent, o.email, float(o.valor), DEFAULT_CHECK] for o in orders]))
            requests.append(update(log_meta['sheetId'], max(1, len(log)), [
                [o.order_id, o.data_hora.isoformat(), o.codent, o.email, float(o.valor),
                 o.status, executed_at.isoformat(), 'RUN'] for o in orders]))
        if requests:
            # Um batchUpdate é atômico: destino, criação da aba e log juntos.
            # Sem retry automático de escrita: timeout pode significar sucesso remoto.
            self.request('POST', self.destination, ':batchUpdate', json={'requests': requests})
        return len(orders)
