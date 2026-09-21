import copy
import io
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest.mock import Mock, patch
from src.filters import select, validate, window
from src.main import pipeline
from src.sheets_client import DEFAULT_CHECK, SheetsClient
from src.utils import Config, ConfigError, DataError
from test_mvp import MemorySheets, record, NOW, TZ


class FarmC1Tests(unittest.TestCase):
    def orders(self):
        return validate([record('c2'), record('c1', codent='100'),
                         record('c3', codent='300'), record('paid', codent='100', status='PAGO')], TZ)

    def mapping(self):
        return {'35175602': 'Farm C2', '100': 'Farm C1', '300': 'Farm C3'}

    def batches(self):
        return {title: select(self.orders(), window(NOW), self.mapping(), set(), farm)[0]
                for farm, title in {'Farm C2': 'NICOLAS', 'Farm C1': 'MARIA'}.items()}

    def test_routing_same_person_different_orders_and_one_batch(self):
        sheets = MemorySheets()
        counts = sheets.commit_batches(self.batches(), NOW)
        self.assertEqual(counts, {'NICOLAS': 1, 'MARIA': 1})
        self.assertEqual(sheets.grids['NICOLAS'][1][1], '35175602')
        self.assertEqual(sheets.grids['MARIA'][1][1], '100')
        self.assertEqual(sheets.grids['MARIA'][1][4], DEFAULT_CHECK)
        self.assertEqual(len(sheets.grids['LOG_AUTOMACAO']), 3)
        self.assertEqual(len(sheets.posts), 1)
        self.assertEqual(sheets.commit_batches(self.batches(), NOW), {'NICOLAS': 0, 'MARIA': 0})
        self.assertEqual(len(sheets.posts), 1)

    def test_legacy_id_not_reinserted_even_after_portfolio_change(self):
        sheets = MemorySheets()
        order = validate([record('old')], TZ)
        sheets.commit(order, NOW)
        self.assertEqual(sheets.commit_batches({'NICOLAS': [], 'MARIA': order}, NOW)['MARIA'], 0)
        self.assertEqual(len(sheets.grids['MARIA']), 1)

    def test_bad_maria_header_aborts_both(self):
        sheets = MemorySheets()
        sheets.grids['MARIA'][0][0] = 'WRONG'
        before = copy.deepcopy(sheets.grids)
        with self.assertRaisesRegex(DataError, 'MARIA'):
            sheets.commit_batches(self.batches(), NOW)
        self.assertEqual(before, sheets.grids)
        self.assertFalse(sheets.posts)

    def test_missing_maria_aborts_both(self):
        sheets = MemorySheets()
        del sheets.grids['MARIA']
        with self.assertRaisesRegex(DataError, 'MARIA'):
            sheets.commit_batches(self.batches(), NOW)
        self.assertFalse(sheets.posts)

    def test_bad_maria_dropdown_aborts_before_any_post(self):
        sheets = MemorySheets()
        def check(name=None):
            if name == 'MARIA': raise DataError('MARIA E2 inválida')
        with patch.object(sheets, 'check_template', side_effect=check), self.assertRaises(DataError):
            sheets.commit_batches(self.batches(), NOW)
        self.assertFalse(sheets.posts)

    def test_never_writes_f_or_old_rows(self):
        sheets = MemorySheets()
        sheets.grids['MARIA'].append(['18/09', '777', 'existing@example.com', 20])
        sheets.commit_batches(self.batches(), NOW)
        for request in sheets.posts[0]['requests']:
            if 'copyPaste' in request:
                body = request['copyPaste']
                self.assertEqual(body['destination']['endColumnIndex'], 5)
                self.assertEqual(body['source']['startColumnIndex'], 4)
            if 'updateCells' in request and request['updateCells']['start']['sheetId'] in (0, 2):
                body = request['updateCells']
                self.assertTrue(all(len(row['values']) == 5 for row in body['rows']))
                self.assertEqual(body['fields'], 'userEnteredValue')
                if body['start']['sheetId'] == 2:
                    self.assertEqual(body['start']['rowIndex'], 2)

    def test_shared_id_between_batches_aborts(self):
        sheets = MemorySheets()
        order = validate([record('same')], TZ)
        with self.assertRaises(DataError): sheets.commit_batches({'NICOLAS': order, 'MARIA': order}, NOW)
        self.assertFalse(sheets.posts)

    def test_empty_c2_does_not_block_c1(self):
        sheets = MemorySheets()
        counts = sheets.commit_batches({'NICOLAS': [], 'MARIA': self.batches()['MARIA']}, NOW)
        self.assertEqual(counts, {'NICOLAS': 0, 'MARIA': 1})

    def test_duplicate_destinations_rejected(self):
        with patch.dict('os.environ', {'DESTINO_SHEET_C1': 'NICOLAS'}), self.assertRaises(ConfigError):
            SheetsClient(Config(), transport=Mock())

    def test_pipeline_test_has_no_writes_and_collects_once(self):
        sheets = MemorySheets()
        del sheets.grids['LOG_AUTOMACAO']
        before = copy.deepcopy(sheets.grids)
        @contextmanager
        def fake_session(*args, **kwargs):
            yield Mock(), Mock(), {}
        output = io.StringIO()
        with patch('src.auth.session', fake_session), \
             patch('src.adm_client.collect', return_value=[record('c2'), record('c1', codent='100')]) as collect, \
             patch('src.sheets_client.SheetsClient', return_value=sheets), \
             patch.object(sheets, 'portfolios', return_value=self.mapping()) as portfolios, \
             patch('src.main.datetime') as clock, redirect_stdout(output):
            clock.now.return_value = NOW
            pipeline(Config(), 'TEST', Mock())
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(portfolios.call_count, 1)
        self.assertIn('Farm C1 → MARIA', output.getvalue())
        self.assertIn('Farm C2 → NICOLAS', output.getvalue())
        self.assertEqual(sheets.grids, before)
        self.assertFalse(sheets.posts)

    def test_log_creation_is_atomic_with_both_sheets(self):
        sheets = MemorySheets()
        del sheets.grids['LOG_AUTOMACAO']
        sheets.commit_batches(self.batches(), NOW)
        self.assertEqual(len(sheets.posts), 1)
        self.assertIn('addSheet', sheets.posts[0]['requests'][0])
        self.assertEqual(len(sheets.grids['LOG_AUTOMACAO']), 3)

    def test_check_template_targets_maria(self):
        sheets = SheetsClient(Config(), transport=Mock())
        body = {'sheets': [{'data': [{'rowData': [
            {'values': [{'userEnteredValue': {'stringValue': 'CHECK'}}]},
            {'values': [{'dataValidation': {'condition': {'type': 'ONE_OF_LIST',
                'values': [{'userEnteredValue': DEFAULT_CHECK}]}}}]}]}]}]}
        with patch.object(sheets, 'request', return_value=body) as request:
            sheets.check_template('MARIA')
        self.assertEqual(request.call_args.kwargs['params']['ranges'], "'MARIA'!E1:E2")

if __name__ == '__main__': unittest.main()
