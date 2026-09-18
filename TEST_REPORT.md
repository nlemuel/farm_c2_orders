# Verificação local — 16/09/2026

Ambiente: Windows, Python 3.13.9, dependências em requirements-tested.txt.

- Testes unitários e integração local: todos aprovados (incluindo Chromium headless real).
- Fixtures de navegador interceptam apenas fixture.test e usam dados fictícios.
- Fontes Python compiladas sem erro.
- Instalador PowerShell analisado pelo parser nativo sem erro.
- Sessão ADM real e credenciais Google não foram fornecidas.
- Login/extração reais, escrita real no Sheets e execução agendada não foram homologados.
- A visita ao ADM retornou /login, “Sem permissão”.
- Nenhum agendamento foi instalado e nenhuma planilha foi alterada.

## Smoke tests de CLI

- `python main.py `: código 2, sessão ausente, sem escrita Google.
- `python main.py --test`: código 2, sessão ausente, sem escrita Google.
- `python main.py --run`: código 2, sessão ausente, sem escrita Google.

## Saída dos testes

```text
test_dom_pagination_real_chromium (test_browser.BrowserTests.test_dom_pagination_real_chromium) ... ok
test_saved_session_reuse_and_expiration (test_browser.BrowserTests.test_saved_session_reuse_and_expiration) ... ok
test_all_pages (test_mvp.ApiTests.test_all_pages) ... ok
test_expiration_mid_collection (test_mvp.ApiTests.test_expiration_mid_collection) ... ok
test_repeated_pages_abort (test_mvp.ApiTests.test_repeated_pages_abort) ... ok
test_atomic_renewal_preserves_previous_on_failure (test_mvp.AuthTests.test_atomic_renewal_preserves_previous_on_failure) ... ok
test_expired_session (test_mvp.AuthTests.test_expired_session) ... ok
test_missing_session (test_mvp.AuthTests.test_missing_session) ... ok
test_url_alone_insufficient (test_mvp.AuthTests.test_url_alone_insufficient) ... ok
test_boundaries_status_portfolio (test_mvp.DomainTests.test_boundaries_status_portfolio) ... ok
test_cli_defaults_and_exclusivity (test_mvp.DomainTests.test_cli_defaults_and_exclusivity) ... ok
test_conflicting_id_fails (test_mvp.DomainTests.test_conflicting_id_fails) ... ok
test_duplicates_same_person_distinct_orders (test_mvp.DomainTests.test_duplicates_same_person_distinct_orders) ... ok
test_invalid_data_aborts (test_mvp.DomainTests.test_invalid_data_aborts) ... ok
test_monday (test_mvp.DomainTests.test_monday) ... ok
test_money_and_identifiers (test_mvp.DomainTests.test_money_and_identifiers) ... ok
test_weekday (test_mvp.DomainTests.test_weekday) ... ok
test_create_log_in_same_batch (test_mvp.SheetsTests.test_create_log_in_same_batch) ... ok
test_destination_only_four_columns (test_mvp.SheetsTests.test_destination_only_four_columns) ... ok
test_expired_session_never_initializes_sheets (test_mvp.SheetsTests.test_expired_session_never_initializes_sheets) ... ok
test_invalid_headers_abort (test_mvp.SheetsTests.test_invalid_headers_abort) ... ok
test_pipeline_run_then_run (test_mvp.SheetsTests.test_pipeline_run_then_run) ... ok
test_portfolios_read_only_needed_columns (test_mvp.SheetsTests.test_portfolios_read_only_needed_columns) ... ok
test_test_mode_never_commits_or_creates_log (test_mvp.SheetsTests.test_test_mode_never_commits_or_creates_log) ... ok
test_three_insertions_then_zero (test_mvp.SheetsTests.test_three_insertions_then_zero) ... ok
test_timeout_does_not_retry (test_mvp.SheetsTests.test_timeout_does_not_retry) ... ok
test_weekend_run_skips_all_access (test_mvp.SheetsTests.test_weekend_run_skips_all_access) ... ok

----------------------------------------------------------------------
Ran 27 tests in 7.445s

OK

```
