"""Testes reais do Chromium contra fixtures locais, habilitados explicitamente."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.adm_client import collect_dom
from src.auth import ensure_session
from src.utils import AuthError, Config, atomic_secret

HTML = '''<div id="private">Pagamentos</div><div id="ready">Pronto</div>
<div id="empty" hidden>Vazio</div><table id="orders"><thead><tr>
<th>ID</th><th>Codent</th><th>Email</th><th>Valor</th><th>Data</th><th>Status</th>
</tr></thead><tbody><tr><td>1</td><td>123</td><td>teste@example.com</td>
<td>10,00</td><td>16/09/2026 10:00</td><td>AGUARDANDO</td></tr></tbody></table>
<button id="next" onclick="document.querySelector('td').textContent='2';
this.disabled=true;">Próxima</button>'''


@unittest.skipUnless(os.environ.get('FARM_BROWSER_TESTS') == '1', 'Defina FARM_BROWSER_TESTS=1 para usar Chromium')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(headless=True, channel='chromium')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def test_dom_pagination_real_chromium(self):
        context = self.browser.new_context()
        try:
            context.route('https://fixture.test/**', lambda route: route.fulfill(body=HTML, content_type='text/html'))
            page = context.new_page()
            page.goto('https://fixture.test/payments')
            adapter = {'authenticated_selector': '#private', 'dom': {
                'table_selector': '#orders', 'ready_selector': '#ready', 'empty_selector': '#empty',
                'next_selector': '#next', 'last_page_selector': '#next:disabled',
                'columns': {'order_id': 'ID', 'codent': 'Codent', 'email': 'Email',
                            'valor': 'Valor', 'data_hora': 'Data', 'status': 'Status'}}}
            with patch.dict(os.environ, {'ADM_URL': 'https://fixture.test/payments'}):
                rows = collect_dom(page, Config(), adapter, Mock())
            self.assertEqual([r['order_id'] for r in rows], ['1', '2'])
        finally:
            context.close()

    def test_saved_session_reuse_and_expiration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'storage_state.json'
            context = self.browser.new_context()
            context.add_cookies([{'name': 'fixture_session', 'value': 'fictional', 'domain': 'fixture.test', 'path': '/'}])
            atomic_secret(path, context.storage_state(indexed_db=True))
            context.close()
            restored = self.browser.new_context(storage_state=str(path))
            def route_handler(route):
                if route.request.url.endswith('/login'):
                    route.fulfill(body='<h1>Login</h1>', content_type='text/html')
                elif 'fixture_session=fictional' in route.request.headers.get('cookie', ''):
                    route.fulfill(body=HTML, content_type='text/html')
                else:
                    route.fulfill(body="<script>location.replace('/login')</script>", content_type='text/html')
            restored.route('https://fixture.test/**', route_handler)
            try:
                page = restored.new_page()
                with patch.dict(os.environ, {'ADM_URL': 'https://fixture.test/payments'}):
                    page.goto('https://fixture.test/payments')
                    ensure_session(page, Config(), {'authenticated_selector': '#private'})
                    restored.clear_cookies()
                    page.goto('https://fixture.test/payments')
                    page.wait_for_url('https://fixture.test/login')
                    with self.assertRaises(AuthError):
                        ensure_session(page, Config(), {'authenticated_selector': '#private'})
            finally:
                restored.close()
