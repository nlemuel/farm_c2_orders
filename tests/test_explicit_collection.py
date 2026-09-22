import unittest
from unittest.mock import Mock, patch
from datetime import datetime
from zoneinfo import ZoneInfo
from src.adm_client import collect_dom, collect_listing
from src.filters import window, validate, select
from src.utils import DataError

class ExplicitTests(unittest.TestCase):
    @patch('src.adm_client.ensure_session')
    @patch('src.adm_client.collect_listing')
    def test_two_tabs(self,listing,auth):
        p=Mock(); p.locator.return_value.count.return_value=1; links=[]
        for q in ('waiting','running'):
            link=Mock(); link.count.return_value=1; link.get_attribute.return_value='/adm-payment?s='+q; links.append(link)
        p.get_by_role.side_effect=links
        listing.side_effect=[[{'order_id':'1'}],[{'order_id':'2'}]]
        self.assertEqual(len(collect_dom(p,Mock(adm_url='https://example.com/adm-payment?old=1'),{'authenticated_selector':'#ok'},Mock(),period=window(datetime(2026,9,21,14,56,tzinfo=ZoneInfo('America/Sao_Paulo'))))),2)
        self.assertEqual([c.args[-1] for c in listing.call_args_list],['AGUARDANDO','EM PROCESSO'])
        self.assertEqual(p.goto.call_args_list[0].args[0],'https://example.com/adm-payment')

    def listing(self,total=1,status='AGUARDANDO',last=True,next_visible=False):
        p=Mock(); loc={s:Mock() for s in ('table','ready','empty','last','next')}
        p.locator.side_effect=lambda s:loc[s]
        loc['ready'].inner_text.return_value=f'Exibidos 1 de {total} resultados'
        loc['empty'].is_visible.return_value=False
        loc['last'].is_visible.return_value=last
        loc['next'].is_visible.return_value=next_visible
        loc['next'].is_enabled.return_value=next_visible
        row=dict(order_id='1',codent='1',email='x@example.com',valor='10',data_hora='19/09/2026 12:00:00',status=status)
        spec={k+'_selector':k for k in ('table','ready','empty','next')};spec['last_page_selector']='last';spec['columns']={k:k for k in row}
        loc['table'].locator.return_value.all_text_contents.return_value=list(row)
        loc['table'].locator.return_value.evaluate_all.return_value=[row]
        with patch('src.adm_client.ensure_session'): return collect_listing(p,Mock(),{'dom':spec},Mock(),'AGUARDANDO')

    def test_complete(self): self.assertEqual(len(self.listing()),1)
    def test_single_page_without_pagination_controls(self):
        self.assertEqual(len(self.listing(last=False)),1)
    def test_missing_controls_must_not_accept_partial_results(self):
        with self.assertRaisesRegex(DataError,'1 de 11'):
            self.listing(total=11,last=False)
    def test_next_after_total_is_inconsistent(self):
        with self.assertRaisesRegex(DataError,'total atingido'):
            self.listing(last=False,next_visible=True)

    def test_incomplete(self):
        with self.assertRaises(DataError): self.listing(188)
    def test_wrong_status(self):
        with self.assertRaises(DataError): self.listing(status='EM PROCESSO')
    def test_weekend_and_dedup(self):
        tz=ZoneInfo('America/Sao_Paulo');period=window(datetime(2026,9,21,14,56,tzinfo=tz))
        self.assertEqual(period[0].strftime('%d/%m/%Y %H:%M'),'18/09/2026 15:00')
        rows=[dict(order_id=str(d*10+i),codent='1',email='x@example.com',valor='10',data_hora=f'{d}/09/2026 '+('15:00:00' if d==18 else '12:00:00'),status=s) for d in range(18,22) for i,s in enumerate(('AGUARDANDO','EM PROCESSO'))]
        fresh,total,dup=select(validate(rows,tz),period,{'1':'Farm C2'},{'180'})
        self.assertEqual((len(fresh),total,dup),(7,8,1))

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit,parse_qs
from unittest.mock import Mock,patch
from src.adm_client import dated_listing_url,checked_next_url,collect_dom,collect_listing,ListingChanged
from src.filters import window
from src.utils import DataError

PERIOD=window(datetime(2026,9,21,14,56,tzinfo=ZoneInfo('America/Sao_Paulo')))
BASE='https://adm.example/adm-payment?pn=status&pni=waiting'

class PeriodTests(unittest.TestCase):
    def test_dates_next_day_and_status_preserved(self):
        url=dated_listing_url(BASE+'&page=8&another_find%5Bdata_end%5D=2020-01-01',PERIOD)
        params=parse_qs(urlsplit(url).query)
        self.assertNotIn('page',params)
        self.assertEqual(params['pni'],['waiting'])
        self.assertEqual(params['another_find[data_init]'],['2026-09-18'])
        self.assertEqual(params['another_find[data_end]'],['2026-09-22'])
    def test_month_boundary(self):
        date=datetime(2026,9,30,tzinfo=ZoneInfo('America/Sao_Paulo'))
        self.assertIn('2026-10-01',dated_listing_url(BASE,(date,date)))
    def test_next_keeps_filters(self):
        first=dated_listing_url(BASE,PERIOD)
        self.assertEqual(checked_next_url(first,first,first+'&page=2'),first+'&page=2')
    def test_bad_next(self):
        first=dated_listing_url(BASE,PERIOD)
        for target in (BASE+'&page=2',first.replace('waiting','running')+'&page=2',first+'&page=3',first.replace('adm.example','evil.example')+'&page=2'):
            with self.subTest(target=target),self.assertRaises(DataError):checked_next_url(first,first,target)
    @patch('src.adm_client.collect_dom_once')
    def test_retry_discards_failed_attempt(self,once):
        once.side_effect=[ListingChanged('20 para 21'),[{'order_id':'ok'}]]
        self.assertEqual(collect_dom(Mock(),Mock(),{},Mock(),period=PERIOD),[{'order_id':'ok'}])
        self.assertEqual(once.call_count,2)
        self.assertEqual(once.call_args.args[-1],PERIOD)
    @patch('src.adm_client.collect_dom_once',side_effect=ListingChanged('20 para 21'))
    def test_retries_bounded(self,once):
        with self.assertRaisesRegex(DataError,'3 tentativas'):collect_dom(Mock(),Mock(),{},Mock(),period=PERIOD)
        self.assertEqual(once.call_count,3)
    @patch('src.adm_client.collect_dom_once',side_effect=DataError('filtro perdido'))
    def test_real_config_error_not_retried(self,once):
        with self.assertRaisesRegex(DataError,'filtro perdido'):collect_dom(Mock(),Mock(),{},Mock(),period=PERIOD)
        self.assertEqual(once.call_count,1)
    @patch('src.adm_client.ensure_session')
    def test_total_change_identifies_status_page_counts(self,auth):
        p=Mock(url=dated_listing_url(BASE,PERIOD))
        loc={s:Mock() for s in ('table','ready','empty','last','next','auth')};p.locator.side_effect=lambda s:loc[s]
        loc['ready'].inner_text.side_effect=['Exibidos 1 de 2 resultados','Exibidos 2 de 3 resultados']
        loc['empty'].is_visible.return_value=False;loc['last'].is_visible.return_value=False
        loc['next'].get_attribute.return_value=p.url+'&page=2'
        row=dict(order_id='1',codent='1',email='x@example.com',valor='10',data_hora='19/09/2026 12:00:00',status='AGUARDANDO')
        spec={k+'_selector':k for k in ('table','ready','empty','next')};spec['last_page_selector']='last';spec['columns']={k:k for k in row}
        loc['table'].locator.return_value.all_text_contents.return_value=list(row)
        loc['table'].locator.return_value.evaluate_all.return_value=[row]
        with self.assertRaisesRegex(ListingChanged,'AGUARDANDO: total mudou de 2 para 3 na página 2'):
            collect_listing(p,Mock(),{'dom':spec,'authenticated_selector':'auth'},Mock(),'AGUARDANDO')

