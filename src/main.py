import argparse
from datetime import datetime
from .utils import AppError, Config, execution_lock, mask_email, setup_log
from .filters import select, validate, window


def parser():
    result = argparse.ArgumentParser(description='Ordens Farm C1/C2 → Google Sheets')
    group = result.add_mutually_exclusive_group()
    group.add_argument('--login', action='store_true', help='Login manual/renovação do ADM')
    group.add_argument('--test', action='store_true', help='Somente leitura (padrão)')
    group.add_argument('--run', action='store_true', help='Escrever lote validado')
    return result


def pipeline(config, mode, logger):
    from .auth import session
    from .adm_client import collect
    from .sheets_client import SheetsClient
    now = datetime.now(config.tz)
    if mode == 'RUN' and now.weekday() >= 5:
        logger.info('fim de semana: execução ignorada')
        print('Fim de semana: --run não executa. Nenhuma alteração realizada.')
        return
    with session(config, login=mode == 'LOGIN') as (context, page, adapter):
        logger.info('sessao valida')
        if mode == 'LOGIN':
            print('Sessão ADM salva com sucesso.')
            return
        period = window(now)
        logger.info('janela=%s ate %s', *[d.isoformat() for d in period])
        raw = collect(context, page, config, adapter, logger, period=period)
        orders = validate(raw, config.tz)
        for status in ('AGUARDANDO', 'EM PROCESSO'):
            found = [o for o in orders if o.status == status]
            inside = [o for o in found if period[0] <= o.data_hora <= period[1]]
            logger.info('status=%s coletadas=%d na_janela=%d', status, len(found), len(inside))
            print(f'{status}: {len(found)} coletadas; {len(inside)} na janela por Última atualização.')
        sheets = SheetsClient(config)
        portfolios = sheets.portfolios()
        processed = sheets.snapshot_for_targets(sheets.routes.values())[3]
        # O modo teste também valida os modelos de CHECK; apenas leituras.
        for title in sheets.routes.values():
            sheets.check_template(title)
        batches = {}
        print(f'\nMODO {mode} — FARM C1/C2\nSessão ADM: OK')
        print(f'Janela: {period[0]:%d/%m/%Y %H:%M} → {period[1]:%d/%m/%Y %H:%M}')
        print(f'Ordens encontradas: {len(raw)}')
        for portfolio, title in sheets.routes.items():
            fresh, eligible, duplicates = select(orders, period, portfolios, processed, portfolio=portfolio)
            batches[title] = fresh
            logger.info('carteira=%s destino=%s elegiveis=%d duplicidades=%d novas=%d',
                        portfolio, title, eligible, duplicates, len(fresh))
            print(f'\n{portfolio} → {title}\nElegíveis: {eligible}\nJá processadas: {duplicates}\nNovas: {len(fresh)}')
            if mode == 'TEST':
                print('DATA | CODENT | E-MAIL (mascarado) | VALOR | CHECK')
                for o in fresh:
                    value = f'{o.valor:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')
                    print(f'{o.data_hora:%d/%m/%Y} | {o.codent} | {mask_email(o.email)} | R$ {value} | NÃO CHAMEI AINDA')
        if mode == 'TEST':
            logger.info('linhas_inseridas=0')
            print('Nenhuma alteração foi realizada nas planilhas.')
        else:
            # Confirma que a sessão não expirou durante a coleta antes de iniciar escrita.
            from .auth import ensure_session, EXPIRED
            from .utils import AuthError
            response = page.goto(config.adm_url, wait_until='domcontentloaded')
            if response and response.status in (401, 403):
                raise AuthError(EXPIRED)
            try:
                page.locator(adapter['authenticated_selector']).wait_for(state='visible', timeout=15000)
            except Exception:
                pass
            ensure_session(page, config, adapter)
            inserted = sheets.commit_batches(batches, datetime.now(config.tz))
            print('EXECUÇÃO CONCLUÍDA')
            for title, count in inserted.items():
                logger.info('destino=%s linhas_inseridas=%d', title, count)
                print(f'{title}: {count} novas inseridas')
            print('Google Sheets: OK\nLOG_AUTOMACAO: OK')


def main(argv=None):
    args = parser().parse_args(argv)
    config = Config()
    logger = None
    try:
        from dotenv import load_dotenv
        load_dotenv(config.root / '.env')
        logger = setup_log(config)
        mode = 'LOGIN' if args.login else 'RUN' if args.run else 'TEST'
        logger.info('inicio modo=%s', mode)
        with execution_lock(config.root):
            pipeline(config, mode, logger)
        return 0
    except AppError as exc:
        if logger:
            logger.error('falha tipo=%s codigo=%d', type(exc).__name__, exc.code)
        print(str(exc))
        return exc.code
    except KeyboardInterrupt:
        print('Execução cancelada. Se a escrita já começou, confira --test antes de repetir.')
        return 130
    except Exception as exc:
        # Não imprime traceback/mensagens de bibliotecas: podem incluir headers/URLs/dados.
        if logger:
            logger.error('falha inesperada tipo=%s', type(exc).__name__)
        print('Falha técnica. Verifique dependências, configuração e conectividade. Detalhes sensíveis foram omitidos.')
        return 1
    finally:
        if logger:
            logger.info('fim')
