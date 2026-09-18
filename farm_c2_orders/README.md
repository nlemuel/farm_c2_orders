# Farm C2 — ordens do ADM para Google Sheets

MVP Python local, sem banco de dados, dashboard ou servidor. Implementa login manual, reutilização de sessão, coleta por API autenticada ou tabela DOM, filtro Farm C2, simulação e escrita atômica de destino + log.

**Estado da entrega:** código implementado; validação de produção pendente. Em 16/09/2026, o acesso a `https://adm.melhorescola.com.br/adm-payment` redirecionou para `/login` com “Sem permissão”. Não houve acesso à tabela protegida, aos endpoints internos ou às planilhas reais. Não há endpoints/seletores inventados: o adaptador vem desativado e precisa ser calibrado na sessão autorizada. Nenhuma tarefa Windows foi instalada e nenhuma planilha foi alterada durante a entrega.

## Primeira configuração

Requer Python 3.11+ e uma pasta local autorizada, fora de pastas públicas ou sincronização compartilhada. No PowerShell, dentro desta pasta:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium --no-shell
Copy-Item .env.example .env
Copy-Item adm_adapter.example.json adm_adapter.json
.\.venv\Scripts\Activate.ps1
```

Se a política do Windows impedir a ativação, use `.\.venv\Scripts\python.exe` em vez de `python`; não é necessário alterar a política de execução.

Antes do primeiro login, configure o seletor de autenticação conforme a seção de calibração. Configure também o acesso Google.

```powershell
python main.py --login
```

Faça login no navegador oficial que abrir. Abra a página de pagamentos. O navegador fecha apenas depois da validação e gravação da sessão. Há 5 minutos para concluir, configuráveis no `.env`.

Depois de calibrar o coletor:

```powershell
python main.py --test
```

Confira os resultados. Quando estiver validado:

```powershell
python main.py --run
```

Sem argumento, `python main.py` executa o modo teste. Os três argumentos são mutuamente exclusivos. `--login` trata exclusivamente da sessão ADM; nunca pede senha no terminal nem utiliza senha no `.env`.

## Calibração obrigatória do ADM

Etapa técnica única que depende do Nicolas ou de alguém autorizado a ver o ADM. Não basta alterar `verified` para `true`.

1. Abra o ADM no navegador usual, autentique-se manualmente e acesse pagamentos. Inspecione um elemento estável que só exista nessa página autenticada. Copie seu seletor para `authenticated_selector` em `adm_adapter.json`. O script exige simultaneamente o mesmo host, o caminho da página de pagamentos e esse elemento visível. Não use seletores genéricos como `body`, `form` ou um menu público.
2. Nas ferramentas do navegador, aba Network, observe Fetch/XHR **depois de autenticar**, aplicando os filtros AGUARDANDO/EM PROCESSO e avançando a paginação. Não exporte HAR, cookies, headers Authorization, tokens, senhas nem respostas com dados pessoais para o projeto/chat. Registre somente o contrato não sensível: URL sem query, parâmetros públicos, caminhos de campos, semântica de datas/valores e paginação.
3. Se houver um endpoint GET simples que use os cookies da sessão, prefira `mode: "api"`. Configure `api.url` no mesmo host HTTPS do ADM, `page_param`, `first_page`, `items_path`, `total_pages_path`, `params` e os seis caminhos de `fields`. Caminhos usam ponto, por exemplo `data.items`; índices de listas podem ser numéricos. `total_pages_path` deve conter **quantidade total de páginas inteira**, não total de registros. Valores monetários devem estar em reais, não centavos; datas ISO ou brasileiras com hora. Nenhum endpoint fictício foi fornecido como se fosse real.
4. A API reutiliza `context.request`, com os cookies do Playwright. Não copia token para `.env` e não adiciona Authorization. Endpoints POST, outro host, tokens exclusivos em localStorage e paginação por cursor exigem adaptar o código após verificar o contrato real; não são automaticamente suportados. Se não existir um GET compatível, use DOM.
5. No modo `dom`, configure seletores estáveis da tabela, `ready_selector` (estado já carregado), `empty_selector` (estado confirmado sem resultados), `next_selector` e `last_page_selector` (somente visível na última página, por exemplo um próximo desabilitado). `columns` contém os textos **exatos** de seis cabeçalhos `thead th`. A tabela deve usar `tbody tr/td`, sem linhas expansíveis extras. `single_page: true` só se comprovadamente não existir paginação. Tabelas virtualizadas, ordem em link/detalhes e filtros obrigatórios exigem ajustar o coletor ao DOM observado.
6. O coletor padrão percorre todas as páginas da listagem e filtra localmente. Confirme que a página inicial não tem um filtro padrão que esconda ordens da janela. Se houver, implemente sua aplicação em `collect_dom` ou configure parâmetros observados em `api.params`. O MVP não presume nomes de filtros nem os aplica por tentativa.
7. Compare a coleta com uma amostra autorizada do ADM, incluindo duas páginas, zero resultados, ambos os status, data/hora, valor e ID. Só então marque `verified: true`. Execute `--test` e confira quantitativos com a fonte antes do primeiro `--run`.

Sem seletor autenticado, até `--login` é bloqueado: sem conhecer um recurso protegido, não seria seguro afirmar que o login foi validado. O template é um contrato de configuração, não um mapeamento já homologado do ADM.

## Acesso Google

Preferência: Service Account aprovada pela empresa.

1. Em um projeto Google Cloud autorizado, habilite Google Sheets API e crie a Service Account.
2. Crie uma chave JSON e armazene apenas em `credentials/google-service-account.json` (crie a pasta). Não cole seu conteúdo no chat/código.
3. Compartilhe a planilha de carteiras com o e-mail da Service Account como **Leitor**; a de destino como **Editor**. Não torne planilhas públicas. A conta precisa conseguir criar `LOG_AUTOMACAO` e escrever nas áreas de destino; proteções de intervalo podem impedir a operação.
4. Mantenha `GOOGLE_AUTH_MODE=service_account`. IDs e abas já estão no `.env.example`.

O escopo da API permite escrita em planilhas, mas a Service Account só acessa os arquivos que lhe forem compartilhados. A aplicação não usa Drive API nem altera compartilhamentos. Pessoas que já têm acesso ao destino verão a atualização normal do Sheets.

Se a empresa não permitir Service Account, use OAuth Desktop aprovado:

1. Configure consentimento OAuth e cliente do tipo aplicativo Desktop no Google Cloud.
2. Salve o cliente em `credentials/google-oauth-client.json`.
3. Defina `GOOGLE_AUTH_MODE=oauth` no `.env`.
4. Execute `python scripts/google_oauth_login.py` e autorize no navegador. Esse comando separado é apenas configuração Google.

O token fica em `auth/google-token.json`, ignorado pelo Git, e é renovado localmente quando possível. `--run` nunca abre consentimento Google; se a renovação falhar, aborta. O escopo OAuth `spreadsheets` abrange as planilhas acessíveis à identidade autorizada; use uma identidade corporativa com acesso mínimo. Consentimento em modo Testing pode ter prazo de refresh token reduzido; valide a configuração com o administrador.

## Regras e escrita segura

- Codent identifica a carteira; somente `Carteira Revenue == "Farm C2"` segue. Espaços externos são removidos. Codent numérico terminado em `.0` é normalizado, mas zeros iniciais em texto são preservados. Formate identificadores como texto para evitar perda de zeros/precisão no Sheets.
- São mantidos apenas AGUARDANDO e EM PROCESSO. PAGO e outros status são ignorados. Dados inválidos de ordens elegíveis abortam o lote, sem escrita parcial.
- Segunda: sexta 15:00 até segunda 14:55; terça a sexta: dia anterior 15:00 até hoje 14:55, sempre America/Sao_Paulo. Limites inclusivos: **14:55:00 é o último instante**. Por fidelidade ao requisito, o intervalo após 14:55:00 e antes de 15:00:00 fica fora; não há recuperação automática dessas ordens. Feriados não têm tratamento especial.
- `--run` no sábado/domingo termina sem escrita. `--test` no fim de semana usa a última janela útil (sexta). Antes de 14:55, o teste/execução mostra somente ordens já existentes na janela do dia. Execuções perdidas não são recuperadas automaticamente; revisar manualmente o período antes de qualquer backfill.
- Deduplicação por ORDER_ID, incluindo repetidos dentro da extração. IDs repetidos com dados diferentes abortam. Ordens diferentes da mesma pessoa permanecem legítimas.
- `NICOLAS` deve existir, com A1:D1 exatamente `DATA DA ORDEM`, `CODENT`, `E-MAIL`, `VALOR`. O script começa após a última linha com conteúdo ou fórmula em A:D; não preenche buracos. A data é texto `dd/mm/aaaa`, valor é número em reais, Codent/e-mail são texto literal. O log guarda a data/hora completa. Não altera formatos existentes.
- Não insere/desloca linhas. Escreve apenas os quatro valores A:D e preserva E em diante, inclusive fórmulas. Caso necessário, aumenta a capacidade de linhas no fim da aba.
- A criação de LOG_AUTOMACAO, seu cabeçalho, as novas linhas do destino e o log são enviados em **um único `spreadsheets.batchUpdate` atômico**. Não há intervalo em que apenas uma das abas tenha sido gravada por esta chamada. Se não houver ordens novas, `--run` ainda cria/inicializa o log caso ausente; `--test` jamais o cria.
- Em timeout de escrita, não há retry cego. Execute `--test` para reler o log antes de repetir. A releitura anterior à escrita e o lock local protegem execuções da mesma pasta. **Não há lock distribuído:** use uma única instalação/escritor e não edite A:D nem o log durante a execução. Duas máquinas, cópias distintas da pasta ou um editor concorrente podem disputar as mesmas linhas.
- Linhas antigas de NICOLAS sem ORDER_ID no log não podem ser deduplicadas com segurança. Antes da primeira produção, reconcilie IDs antigos no log ou use uma janela sem sobreposição com os dados históricos. Não use apenas Codent + e-mail para essa reconciliação.

## Sessão e segurança

`auth/storage_state.json` contém cookies/localStorage/IndexedDB e equivale a uma credencial. Não contém senha coletada pelo script; o script não registra tráfego de login. A renovação abre um contexto novo e só substitui o arquivo anterior após confirmar a área protegida. Falhas/cancelamento preservam o arquivo anterior. O ADM pode invalidar a sessão anterior no servidor independentemente do arquivo local.

O storage state é reutilizado em novos contextos, sem um perfil de navegador permanente. Sessões dependentes exclusivamente de sessionStorage, de vínculo ao dispositivo ou de políticas adicionais precisam de validação real e podem não funcionar com esse mecanismo.

`.gitignore` não criptografa arquivos. Mantenha `.env`, `auth/`, `credentials/` e `logs/` apenas no computador autorizado, com ACL NTFS restrita ao usuário da tarefa e administradores autorizados. O `chmod` aplicado aos temporários não substitui ACL no Windows. Use a proteção de disco aprovada pela empresa. Não habilite trace, vídeo, screenshots automáticos ou logs HTTP de depuração em produção.

Log local: `logs/YYYY-MM-DD.log`, com modo, sessão, janela, páginas, contagens, código/tipo de erro e fim. Não guarda payloads, tokens, senhas ou e-mails. O terminal de teste mascara e-mails; as planilhas recebem os e-mails completos requeridos. Logs locais e locks podem ser escritos em `--test`; **nenhuma escrita remota nas planilhas** é realizada nesse modo. Tokens OAuth podem ser renovados localmente.

## Agendador de Tarefas do Windows

Instale o agendamento somente depois de homologar `--test` e a primeira execução real. Verifique o fuso do Windows em Brasília. O Python usa America/Sao_Paulo independentemente do fuso do sistema, mas o horário do Agendador depende do Windows.

```powershell
.\scripts\install_schedule.ps1
```

O script cria uma tarefa semanal, segunda a sexta às 14:56, com o Python da `.venv`, argumentos `main.py --run` e diretório de trabalho correto. Usa a identidade atual, sem privilégios elevados, evita sobreposição e não substitui uma tarefa existente. Não agenda `--login`. O arquivo `scripts/run_production.bat` é alternativa para execução manual ou ação no Agendador; preserva o código de saída.

Configuração manual equivalente: programa = caminho absoluto de `.venv\Scripts\python.exe`; argumentos = `main.py --run`; “Iniciar em” = pasta absoluta do projeto. Configure repetição semanal de segunda a sexta às 14:56 e “Não iniciar uma nova instância”. Não use reinício automático em falhas de escrita incerta.

- **Usuário conectado e computador bloqueado:** a tarefa padrão funciona em headless, sem janela visível. Teste com o Windows bloqueado.
- **Usuário desconectado:** o instalador usa logon interativo e não cobre esse caso. No Agendador, configure “Executar independentemente de o usuário estar conectado” com a conta autorizada. Informe eventuais credenciais Windows somente na interface oficial do Windows. A conta precisa de logon em lote, acesso local às pastas/Chromium, rede e permissões corporativas. Evite opção sem armazenamento de senha se ela impedir acesso à rede. Não escreva senha Windows no script.
- **Computador desligado/suspenso/sem rede:** execução não é garantida. Mantenha ligado e conectado; configure políticas de energia com o suporte se necessário. O instalador não habilita wake nem execução tardia, para evitar processar outra janela sem revisão.

## Uso diário

Depois da configuração e instalação do agendamento, nenhuma ação cotidiana é necessária: segunda a sexta às 14:56. Confirme inicialmente “Último Resultado” no Agendador e as contagens do log.

Se a sessão expirar:

```powershell
python main.py --login
python main.py --test
```

Faça login novamente. A automação fica pronta após a validação. Não existe renovação automática por senha.

## Códigos de saída

| Código | Significado |
|---|---|
| 0 | Sucesso, simulação ou fim de semana ignorado |
| 1 | Falha técnica inesperada; detalhes sensíveis omitidos |
| 2 | Sessão ausente/expirada/cancelada; também erro de argumentos da CLI |
| 3 | Configuração, permissões Google ou validação de área protegida |
| 4 | Dados/esquema/paginação inconsistentes; lote abortado |
| 5 | Resultado de escrita incerto; reler log antes de repetir |
| 6 | Outra execução da mesma pasta está ativa |
| 130 | Interrompido pelo usuário |

## Testes e homologação

```powershell
python -m unittest discover -s tests -v
python -m compileall -q main.py src scripts tests
```

Para incluir os testes de paginação DOM e storage state com Chromium real (páginas fictícias, sem rede para o ADM):

```powershell
$env:FARM_BROWSER_TESTS = '1'
python -m unittest discover -s tests -v
```

Os testes usam dados fictícios e transportes simulados, sem credenciais, sem acesso ao ADM e sem escrita Google. Cobrem janela de segunda e dia comum, limites, status, carteiras C1/C2/C3, normalização, IDs divergentes, duplicidade, preservação da sessão, sessão inexistente/expirada, teste sem escrita, paginação API, criação atômica de log e três inserções seguidas de zero.

Homologação real ainda necessária:

1. Login inicial, reutilização e renovação cancelada preservando sessão anterior.
2. Sessão realmente expirada: código 2 e nenhuma alteração remota.
3. Endpoint/DOM e paginação calibrados; conferir amostra e total com ADM.
4. Acesso Google mínimo e `--test` com zero mudanças nas duas abas.
5. Três ordens autorizadas: três inserções, novo `--run`: zero e três IDs já processados; verificar E em diante preservadas.
6. Agendador às 14:56 com máquina bloqueada e, se desejado, usuário desconectado.

Não marque os critérios de aceite de produção como concluídos apenas pelos testes simulados. Esta entrega não contém credenciais, sessão válida ou homologação real.

## Estrutura

`main.py` é o ponto de entrada na raiz; `src/main.py` coordena a CLI; `auth.py`, `adm_client.py`, `sheets_client.py`, `filters.py` e `utils.py` separam responsabilidades. `scripts/` contém execução/agendamento e OAuth opcional; `tests/` contém testes. Pastas `auth/` e `logs/` são criadas quando necessárias; crie `credentials/` ao instalar a chave.

Dependências têm faixas de versão para limitar atualizações incompatíveis. `requirements-tested.txt` registra as versões exatas instaladas no ambiente de teste Python 3.13/Windows; pode ser usado com `pip install -r requirements-tested.txt` para reprodução. Homologue as versões instaladas antes de atualizar a máquina de produção.

Referências oficiais: [autenticação Playwright](https://playwright.dev/python/docs/auth), [requisições autenticadas Playwright](https://playwright.dev/python/docs/api-testing), [atomicidade de batchUpdate do Sheets](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate).
