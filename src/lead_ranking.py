"""Native row sorting; never rewrite the team's CHECK, seller or notes."""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from copy import deepcopy
from .utils import DataError, money

STATUS_HEADER = 'STATUS ADM'
STATUS_NAMES = {'AGUARDANDO': 'Aguardando', 'EM PROCESSO': 'Em Processo'}
EPOCH = date(1899, 12, 30)


def value(cell):
    data = cell.get('effectiveValue') or cell.get('userEnteredValue', {})
    return next(iter(data.values()), '')


def date_value(raw, legacy_year=2026):
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return EPOCH + timedelta(days=int(raw))
    text = str(raw).strip()
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    # Legacy manual rows in this workbook were entered as dd/mm in 2026.
    try:
        return datetime.strptime(f'{text}/{legacy_year}', '%d/%m/%Y').date()
    except ValueError:
        raise DataError('Data inválida para ranking; informe dia/mês/ano na coluna A.') from None


def key(day, code, email, amount):
    code = str(code).strip()
    if code.endswith('.0'):
        code = code[:-2]
    return day, code, str(email).strip().casefold(), money(amount)


def log_index(log):
    result = defaultdict(list)
    for row in log[1:]:
        if len(row) < 6 or not row[0]:
            continue
        try:
            day = datetime.fromisoformat(str(row[1])).date()
            item = key(day, row[2], row[3], row[4])
        except (ValueError, DataError):
            continue
        result[item].append(str(row[5]).strip().upper())
    return result


def put(sheet, row, col, val, pattern=None):
    cell = {'userEnteredValue': {'numberValue' if isinstance(val, (float, int)) else 'stringValue': val}}
    fields = 'userEnteredValue'
    if pattern:
        cell['userEnteredFormat'] = {'numberFormat': {'type': 'DATE', 'pattern': pattern}}
        fields += ',userEnteredFormat.numberFormat'
    return {'updateCells': {'start': {'sheetId': sheet, 'rowIndex': row, 'columnIndex': col},
                            'rows': [{'values': [cell]}], 'fields': fields}}


def plan_sheet(sheet, rows, log, fresh=(), legacy_year=2026):
    """rows: CellData, bounded to the existing populated A:D rectangle."""
    props = sheet['properties']; sid = props['sheetId']; requests = []
    header = [str(value(c)).strip() for c in rows[0]['values']]
    matches = [i for i, h in enumerate(header) if h == STATUS_HEADER]
    if len(matches) > 1:
        raise DataError('STATUS ADM duplicado; nenhuma escrita iniciada.')
    status_col = matches[0] if matches else max(8, max((i + 1 for i, h in enumerate(header) if h), default=0))
    if status_col < 8:
        raise DataError('STATUS ADM deve ficar após as colunas da equipe.')
    columns = props['gridProperties']['columnCount']
    if status_col >= columns:
        requests.append({'appendDimension': {'sheetId': sid, 'dimension': 'COLUMNS', 'length': status_col + 1 - columns}})
        columns = status_col + 1
    if not matches:
        if any(len(r.get('values', [])) > status_col and r['values'][status_col].get('userEnteredValue') for r in rows[1:]):
            raise DataError('A coluna reservada ao STATUS ADM contém dados sem cabeçalho.')
        requests += [
            {'copyPaste': {'source': {'sheetId': sid, 'startRowIndex': 0, 'endRowIndex': 1, 'startColumnIndex': status_col - 1, 'endColumnIndex': status_col},
                           'destination': {'sheetId': sid, 'startRowIndex': 0, 'endRowIndex': 1, 'startColumnIndex': status_col, 'endColumnIndex': status_col + 1}, 'pasteType': 'PASTE_FORMAT'}},
            put(sid, 0, status_col, STATUS_HEADER),
            {'repeatCell': {'range': {'sheetId': sid, 'startRowIndex': 0, 'endRowIndex': 1, 'startColumnIndex': status_col, 'endColumnIndex': status_col + 1},
                            'cell': {'note': 'Status do ADM no momento da coleta. Linhas antigas sem correspondência única no LOG_AUTOMACAO permanecem vazias; não é monitoramento em tempo real.'}, 'fields': 'note'}},
            {'updateDimensionProperties': {'range': {'sheetId': sid, 'dimension': 'COLUMNS', 'startIndex': status_col, 'endIndex': status_col + 1}, 'properties': {'pixelSize': 165}, 'fields': 'pixelSize'}}]
    lookup = log_index(log); keys = []
    for row in rows[1:]:
        cells = row.get('values', [])
        vals = [value(c) for c in cells[:4]]
        if len(vals) < 4 or any(v == '' for v in vals):
            raise DataError(f'Linha sem data/código/email/valor na aba {props["title"]}; ranking abortado.')
        keys.append(key(date_value(vals[0], legacy_year), *vals[1:]))
    occurrences = Counter(keys)
    filled = 0
    for index, (row, item) in enumerate(zip(rows[1:], keys), 1):
        cells = row['values']
        for col in (0, 3):
            entered = cells[col].get('userEnteredValue', {})
            if 'stringValue' in entered:
                if col == 0:
                    pattern = 'dd/mm' if entered['stringValue'].strip().count('/') == 1 else 'dd/mm/yyyy'
                    requests.append(put(sid, index, col, (item[0] - EPOCH).days, pattern))
                else:
                    requests.append(put(sid, index, col, float(item[3])))
            elif not isinstance(value(cells[col]), (int, float)):
                raise DataError('Fórmula de data/valor sem resultado numérico; ranking abortado.')
        current = cells[status_col] if len(cells) > status_col else {}
        statuses = lookup.get(item, [])
        if not current.get('userEnteredValue') and occurrences[item] == 1 and len(statuses) == 1 and statuses[0] in STATUS_NAMES:
            if current.get('dataValidation'):
                raise DataError('STATUS ADM possui validação inesperada; revise antes de preencher.')
            requests.append(put(sid, index, status_col, STATUS_NAMES[statuses[0]])); filled += 1
    for index, order in enumerate(fresh, len(rows)):
        requests.append(put(sid, index, 0, (order.data_hora.date() - EPOCH).days, 'dd/mm/yyyy'))
        requests.append(put(sid, index, status_col, STATUS_NAMES[order.status]))
    end = len(rows) + len(fresh)
    # Extend the existing green rule without changing the yellow/orange rules.
    green = None
    for i, rule in enumerate(sheet.get('conditionalFormats', [])):
        cond = rule.get('booleanRule', {}).get('condition', {})
        ranges = rule.get('ranges', [])
        text = ' '.join(v.get('userEnteredValue', '') for v in cond.get('values', []))
        if ranges and all(r.get('startColumnIndex') == 3 and r.get('endColumnIndex') == 4 for r in ranges) and (cond.get('type') == 'NUMBER_GREATER' and text == '1000' or cond.get('type') == 'CUSTOM_FORMULA' and 'D2>1000' in text.replace(' ', '')):
            green = i
            if any('endRowIndex' in r for r in ranges):
                extended = deepcopy(rule)
                for r in extended['ranges']: r.pop('endRowIndex', None)
                requests.append({'updateConditionalFormatRule': {'sheetId': sid, 'index': i, 'rule': extended}})
            break
    if green is None:
        requests.append({'addConditionalFormatRule': {'index': 0, 'rule': {'ranges': [{'sheetId': sid, 'startRowIndex': 1, 'startColumnIndex': 3, 'endColumnIndex': 4}], 'booleanRule': {'condition': {'type': 'NUMBER_GREATER', 'values': [{'userEnteredValue': '1000'}]}, 'format': {'backgroundColor': {'red': 0.8509804, 'green': 0.9176471, 'blue': 0.827451}}}}}})
    if end > 2:
        requests.append({'sortRange': {'range': {'sheetId': sid, 'startRowIndex': 1, 'endRowIndex': end, 'startColumnIndex': 0, 'endColumnIndex': columns},
                                      'sortSpecs': [{'dimensionIndex': 0, 'sortOrder': 'ASCENDING'}, {'dimensionIndex': 3, 'sortOrder': 'DESCENDING'}]}})
    return requests, {'status_column': status_col, 'backfilled': filled, 'rows': end - 1}
