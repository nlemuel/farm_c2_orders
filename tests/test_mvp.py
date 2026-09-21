import copy
import io
import json
import logging
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from src.auth import authenticated, ensure_session, session
from src.filters import select, validate, window
from src.main import main, parser, pipeline
from src.sheets_client import DEST_HEADERS, LOG_HEADERS, SheetsClient
from src.utils import AuthError, Config, ConfigError, DataError, WriteUncertain, atomic_secret, identifier, money
from src.adm_client import collect_api

TZ = ZoneInfo('America/Sao_Paulo')
NOW = datetime(2026, 9, 16, 14, 56, tzinfo=TZ)


def record(oid='123', **kw):
    return dict(order_id=oid, codent='35175602', email='aluno@example.com', valor='R$ 1.317,36',
                data_hora='16/09/2026 10:30', status='AGUARDANDO') | kw


class MemorySheets(SheetsClient):
    def __init__(self):
        super().__init__(Config(), transport=Mock())
        self.grids = {'NICOLAS': [list(DEST_HEADERS)], 'LOG_AUTOMACAO': [list(LOG_HEADERS)], 'MARIA': [list(DEST_HEADERS)]}
        self.posts = []

    def check_template(self, sheet_name=None):
        pass

    def metadata(self):
        return {name: {'title': name, 'sheetId': i, 'gridProperties': {'rowCount': 1000, 'columnCount': 20}}
                for i, name in enumerate(self.grids)}

    def values(self, spreadsheet, title, columns):
        return copy.deepcopy(self.grids[title])

    def request(self, method, spreadsheet, suffix='', **kwargs):
        assert method == 'POST'
        self.posts.append(kwargs['json'])
        ids = {v['sheetId']: k for k, v in self.metadata().items()}
        for req in kwargs['json']['requests']:
            if 'addSheet' in req:
                props = req['addSheet']['properties']
                ids[props['sheetId']] = props['title']
                self.grids[props['title']] = []
            if 'updateCells' in req:
                body = req['updateCells']
                grid = self.grids[ids[body['start']['sheetId']]]
                for index, row in enumerate(body['rows'], body['start']['rowIndex']):
                    while len(grid) <= index:
                        grid.append([])
                    grid[index] = [next(iter(c['userEnteredValue'].values())) for c in row['values']]
        return {}


class DomainTests(unittest.TestCase):
    def test_monday(self):
        start, end = window(datetime(2026, 9, 14, 14, 56, tzinfo=TZ))
        self.assertEqual(start.isoformat(), '2026-09-11T15:00:00-03:00')
        self.assertEqual(end.isoformat(), '2026-09-14T14:55:00-03:00')

    def test_weekday(self):
        self.assertEqual(window(NOW)[0].isoformat(), '2026-09-15T15:00:00-03:00')

    def test_boundaries_status_portfolio(self):
        orders = validate([record('1', data_hora='15/09/2026 15:00'),
                           record('2', data_hora='16/09/2026 14:55'),
                           record('3', data_hora='16/09/2026 14:55:01'),
                           record('4', status='PAGO'), record('5', codent='2'),
                           record('6', codent='3'), record('7', status='EM PROCESSO')], TZ)
        new, total, dup = select(orders, window(NOW), {'35175602': 'Farm C2', '2': 'Farm C1', '3': 'Farm C3'}, {'1'})
        self.assertEqual({o.order_id for o in new}, {'2', '7'})
        self.assertEqual((total, dup), (3, 1))

    def test_duplicates_same_person_distinct_orders(self):
        self.assertEqual(len(validate([record('1'), record('1'), record('2')], TZ)), 2)

    def test_conflicting_id_fails(self):
        with self.assertRaises(DataError):
            validate([record(), record(valor='10,00')], TZ)

    def test_invalid_data_aborts(self):
        for fields in ({'email': 'bad'}, {'valor': 'NaN'}, {'valor': '1.001'}, {'data_hora': '16/09/2026'}, {'order_id': ''}):
            with self.subTest(fields=fields), self.assertRaises(DataError):
                validate([record(**fields)], TZ)

    def test_money_and_identifiers(self):
        self.assertEqual(str(money('R$ 1.317,36')), '1317.36')
        self.assertEqual(identifier(35175602.0), '35175602')
        self.assertEqual(identifier('00123'), '00123')

    def test_cli_defaults_and_exclusivity(self):
        args = parser().parse_args([])
        self.assertFalse(args.run or args.login)
        with redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(['--run', '--test'])


class AuthTests(unittest.TestCase):
    def test_missing_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AuthError) as caught:
                with session(Config(Path(tmp))):
                    pass
            self.assertEqual(caught.exception.code, 2)

    def test_expired_session(self):
        page = Mock(url='https://adm.melhorescola.com.br/login')
        with self.assertRaises(AuthError):
            ensure_session(page, Config(), {'authenticated_selector': '#private'})

    def test_url_alone_insufficient(self):
        page = Mock(url=Config().adm_url)
        page.locator.return_value.is_visible.return_value = False
        self.assertFalse(authenticated(page, Config(), {'authenticated_selector': '#private'}))

    def test_atomic_renewal_preserves_previous_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            atomic_secret(path, {'version': 1})
            with patch('src.utils.os.replace', side_effect=OSError), self.assertRaises(OSError):
                atomic_secret(path, {'version': 2})
            self.assertEqual(json.loads(path.read_text()), {'version': 1})
            atomic_secret(path, {'version': 3})
            self.assertEqual(json.loads(path.read_text()), {'version': 3})
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)


class SheetsTests(unittest.TestCase):
    def test_pipeline_run_then_run(self):
        sheets = MemorySheets()
        @contextmanager
        def fake_session(*a, **kw):
            yield Mock(), Mock(), {'authenticated_selector': '#private'}
        with patch('src.auth.session', fake_session), patch('src.auth.ensure_session'), \
             patch('src.adm_client.collect', return_value=[record(str(i)) for i in range(3)]), \
             patch('src.sheets_client.SheetsClient', return_value=sheets), \
             patch.object(sheets, 'portfolios', return_value={'35175602': 'Farm C2'}), \
             patch('src.main.datetime') as clock, redirect_stdout(io.StringIO()):
            clock.now.return_value = NOW
            pipeline(Config(), 'RUN', Mock())
            pipeline(Config(), 'RUN', Mock())
        self.assertEqual(len(sheets.posts), 1)
        self.assertEqual(len(sheets.grids['NICOLAS']), 4)

    def test_expired_session_never_initializes_sheets(self):
        with patch('src.auth.session', side_effect=AuthError('Sessão expirada')), \
             patch('src.sheets_client.SheetsClient') as client, patch('src.main.datetime') as clock:
            clock.now.return_value = NOW
            with self.assertRaises(AuthError):
                pipeline(Config(), 'RUN', Mock())
            client.assert_not_called()

    def test_weekend_run_skips_all_access(self):
        with patch('src.auth.session') as auth, patch('src.sheets_client.SheetsClient') as sheets, \
             patch('src.main.datetime') as clock, redirect_stdout(io.StringIO()):
            clock.now.return_value = datetime(2026, 9, 19, 14, 56, tzinfo=TZ)
            pipeline(Config(), 'RUN', Mock())
            auth.assert_not_called()
            sheets.assert_not_called()

    def test_portfolios_read_only_needed_columns(self):
        sheets = SheetsClient(Config(), transport=Mock())
        with patch.object(sheets, 'values', side_effect=[
            [['Codent', 'Nome da Escola', 'Carteira Revenue', 'Responsável']],
            [[123.0], ['0012'], [4]], [['Farm C2'], ['Farm C1'], ['Farm C3']]
        ]) as reads:
            self.assertEqual(sheets.portfolios(), {'123': 'Farm C2', '0012': 'Farm C1', '4': 'Farm C3'})
        self.assertEqual([c.args[2] for c in reads.call_args_list], ['A1:ZZ1', 'A2:A', 'C2:C'])

    def test_three_insertions_then_zero(self):
        sheets = MemorySheets()
        orders = validate([record(str(i)) for i in range(3)], TZ)
        self.assertEqual(sheets.commit(orders, NOW), 3)
        self.assertEqual(sheets.commit(orders, NOW), 0)
        self.assertEqual(len(sheets.posts), 1)
        self.assertEqual(len(sheets.grids['NICOLAS']), 4)
        self.assertEqual(len(sheets.grids['LOG_AUTOMACAO']), 4)
        new, total, duplicates = select(orders, window(NOW), {'35175602': 'Farm C2'}, sheets.processed())
        self.assertEqual((len(new), total, duplicates), (0, 3, 3))

    def test_create_log_in_same_batch(self):
        sheets = MemorySheets()
        del sheets.grids['LOG_AUTOMACAO']
        sheets.commit(validate([record()], TZ), NOW)
        self.assertEqual(len(sheets.posts), 1)
        self.assertIn('addSheet', sheets.posts[0]['requests'][0])

    def test_destination_only_five_columns(self):
        sheets = MemorySheets()
        sheets.commit(validate([record(email='=x@example.com')], TZ), NOW)
        req = next(r['updateCells'] for r in sheets.posts[0]['requests']
                   if 'updateCells' in r and r['updateCells']['start']['sheetId'] == 0)
        self.assertEqual(len(req['rows'][0]['values']), 5)
        self.assertEqual(req['fields'], 'userEnteredValue')
        self.assertEqual(req['rows'][0]['values'][2], {'userEnteredValue': {'stringValue': '=x@example.com'}})

    def test_test_mode_never_commits_or_creates_log(self):
        sheets = MemorySheets()
        del sheets.grids['LOG_AUTOMACAO']
        before = copy.deepcopy(sheets.grids)
        page = Mock()
        @contextmanager
        def fake_session(*a, **kw):
            yield Mock(), page, {}
        with patch('src.auth.session', fake_session), patch('src.adm_client.collect', return_value=[record()]), \
             patch('src.sheets_client.SheetsClient', return_value=sheets), \
             patch.object(sheets, 'portfolios', return_value={'35175602': 'Farm C2'}), \
             patch('src.main.datetime') as clock, redirect_stdout(io.StringIO()):
            clock.now.return_value = NOW
            pipeline(Config(), 'TEST', Mock())
        self.assertEqual(sheets.grids, before)
        self.assertEqual(sheets.posts, [])

    def test_timeout_does_not_retry(self):
        transport = Mock()
        transport.request.side_effect = TimeoutError()
        sheets = SheetsClient(Config(), transport)
        with self.assertRaises(WriteUncertain):
            sheets.request('POST', sheets.destination, ':batchUpdate', json={})
        self.assertEqual(transport.request.call_count, 1)

    def test_invalid_headers_abort(self):
        sheets = MemorySheets()
        sheets.grids['NICOLAS'][0][0] = 'INVALID'
        with self.assertRaises(DataError):
            sheets.commit(validate([record()], TZ), NOW)
        self.assertFalse(sheets.posts)


class ApiTests(unittest.TestCase):
    def setup_api(self, bodies):
        page = Mock(url=Config().adm_url)
        page.locator.return_value.is_visible.return_value = True
        context = Mock()
        responses = []
        for body in bodies:
            response = Mock(status=200, headers={'content-type': 'application/json'})
            response.json.return_value = body
            responses.append(response)
        context.request.get.side_effect = responses
        adapter = {'authenticated_selector': '#private', 'max_pages': 10, 'api': {
            'url': 'https://adm.melhorescola.com.br/fixture-only', 'page_param': 'page',
            'total_pages_path': 'pages', 'items_path': 'items',
            'fields': {k: k for k in record()}}}
        return context, page, adapter

    def test_all_pages(self):
        context, page, adapter = self.setup_api([{'pages': 2, 'items': [record('1')]}, {'pages': 2, 'items': [record('2')]}])
        result = collect_api(context, page, Config(), adapter, Mock())
        self.assertEqual(len(result), 2)

    def test_repeated_pages_abort(self):
        context, page, adapter = self.setup_api([{'pages': 2, 'items': [record()]}, {'pages': 2, 'items': [record()]}])
        with self.assertRaises(DataError):
            collect_api(context, page, Config(), adapter, Mock())

    def test_expiration_mid_collection(self):
        context, page, adapter = self.setup_api([])
        context.request.get.side_effect = None
        context.request.get.return_value = Mock(status=401)
        with self.assertRaises(AuthError):
            collect_api(context, page, Config(), adapter, Mock())


if __name__ == '__main__':
    unittest.main()
