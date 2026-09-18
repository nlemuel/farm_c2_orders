import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from .utils import DataError, identifier, money, timestamp

ALLOWED = {'AGUARDANDO', 'EM PROCESSO'}


@dataclass(frozen=True)
class Order:
    order_id: str
    data_hora: datetime
    codent: str
    email: str
    valor: Decimal
    status: str


def window(now):
    # Execuções manuais no fim de semana usam a última janela útil (sexta).
    day = now.date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    previous = day - timedelta(days=3 if day.weekday() == 0 else 1)
    return (datetime.combine(previous, time(15), now.tzinfo),
            datetime.combine(day, time(14, 55), now.tzinfo))


def validate(raw, tz):
    orders = {}
    for record_index, record in enumerate(raw, start=1):
        try:
            status = ' '.join(str(record['status']).upper().split())
            if status not in ALLOWED:
                continue
            email = str(record['email']).strip()
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                raise DataError('E-mail inválido em ordem elegível.')
            order = Order(identifier(record['order_id'], field='ORDER_ID', location=f'ADM, registro coletado {record_index}'), timestamp(record['data_hora'], tz),
                          identifier(record['codent'], field='CODENT', location=f'ADM, registro coletado {record_index}'), email, money(record['valor']), status)
        except (KeyError, TypeError):
            raise DataError('Ordem elegível com campos ausentes.') from None
        if order.order_id in orders and orders[order.order_id] != order:
            raise DataError('Mesmo ORDER_ID com dados divergentes; lote abortado.')
        orders[order.order_id] = order
    return list(orders.values())


def select(orders, period, portfolios, processed):
    start, end = period
    eligible = [o for o in orders if start <= o.data_hora <= end
                and portfolios.get(o.codent) == 'Farm C2' and o.status in ALLOWED]
    new = sorted((o for o in eligible if o.order_id not in processed),
                 key=lambda o: (o.data_hora, o.order_id))
    return new, len(eligible), len(eligible) - len(new)
