# NexFiscal

Automação **local (desktop)** para portais municipais: lê uma planilha com várias empresas de várias cidades,
entra em cada portal, baixa os **XMLs de NFS-e** e o **Livro Fiscal** da competência anterior, organiza tudo em
pastas e gera um relatório. A memória dos portais é persistida em SQLite. O modo guiado, ligado por padrão,
reutiliza seletores aprendidos e solicita orientação quando eles deixam de funcionar.

```
PLANILHA → CIDADE → PORTAL → MEMÓRIA → EMPRESA → CNPJ + SENHA → LOGIN → CAPTCHA MANUAL
        → XML → LIVRO FISCAL → ORGANIZAÇÃO → RELATÓRIO
```

> **CAPTCHA:** o NexFiscal **nunca** resolve, contorna ou burla CAPTCHA. Ele detecta, pausa, mantém o navegador
> visível e espera **você** resolver. Depois do CAPTCHA, **Entrar/Acessar e qualquer envio do formulário
> continuam manuais**, inclusive em retentativas. Resolver ou confirmar o CAPTCHA não autoriza o envio automático.

## Requisitos

- Python **3.12+**
- Windows, Linux ou macOS (a interface usa PySide6)
- Acesso de rede aos portais e uma conta/credencial válida em cada um

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip==26.2.2
python -m pip install -r requirements.txt
python -m playwright install chromium   # navegador usado pela automação
```

Navegador já instalado? Em *Configurações* informe o canal (`chrome` ou `msedge`) e dispense o download.

As versões verificadas estão em `constraints.txt`, aplicado automaticamente pelos arquivos de requisitos.
Atualizações exigem repetir os testes e a auditoria de dependências. A CI verifica Python 3.12 em Linux e Windows.

## Como iniciar

```bash
python main.py            # interface gráfica
python main.py --help     # linha de comando (mesmas funções)
```

## Planilha de entrada

`.xlsx` com quatro colunas obrigatórias (aceita variações como `CNPJ`, `Usuário`, `Município`, `Empresa` e `Nome Empresarial`):

| CNPJ/USUÁRIO       | RAZÃO SOCIAL                          | SENHA      | CIDADE           |
| ------------------ | ------------------------------------- | ---------- | ---------------- |
| 12.345.678/0001-01 | RAZÃO SOCIAL COMPLETA DA EMPRESA LTDA | Senha@001  | Salvador         |
| 45.678.901/0001-04 | OUTRA EMPRESA LTDA                    | Acesso#456 | Feira de Santana |

O CNPJ/USUÁRIO é o login do portal. A coluna RAZÃO SOCIAL é obrigatória e nomeia a pasta da empresa; CNPJ e senha não são usados no nome das pastas. Antes de processar, a planilha é validada (arquivo, colunas, campos vazios, formato do CNPJ, duplicidades, cidades sem portal). Enquanto houver **erros críticos** nada é executado; o relatório de erros usa o **número da linha do Excel**:

```
ERROS ENCONTRADOS

Linha 8:
- CNPJ/USUÁRIO vazio
```

Linha idêntica repetida vira aviso (processada uma vez); mesma empresa com senha ou razão social diferente é erro. Cidade sem portal cadastrado é **aviso**: a URL é pedida antes do processamento.


## Cadastrar uma cidade/portal

- **GUI:** ao processar, se faltar o portal, abre a pergunta *“Informe a URL do portal”*. Ou use
  *Configurações → Gerenciar portais*.
- **CLI:** `python main.py portal add "Salvador,Lauro de Freitas" https://portal.da.prefeitura.gov.br`
  (várias cidades podem apontar para o mesmo portal) · `python main.py portal list`

O cadastro é permanente. O NexFiscal **não traz URLs prontas**: informe as do seu município.

## Competência

Padrão: **mês anterior** (execução em 06/10/2026 → `09/2026`, 01/09 a 30/09). Trata virada de ano, fevereiro e
bissexto. Pode informar `MM/AAAA` na tela principal, e datas inicial/final manuais em *Configurações* ou
`--inicio/--fim`.

## Como resolver o CAPTCHA

1. O navegador **permanece visível** e o painel mostra *CAPTCHA DETECTADO — Empresa/Cidade — Resolva o CAPTCHA no navegador*.
2. Resolva-o e clique manualmente em **Entrar/Acessar** se o portal exigir o envio do formulário.
3. O NexFiscal percebe a liberação e segue. Se o portal recusar o código, ele pede de novo. Tempo máximo
   configurável (padrão 10 min). O modo *headless* é incompatível com CAPTCHA e é recusado.

## Estrutura de saída

```
ROOT/                                  (única pasta configurável)
└── Salvador/
    └── RAZÃO SOCIAL COMPLETA DA EMPRESA LTDA/
        ├── nome-original-do-portal.xml
        └── Livro Fiscal.pdf
```

A estrutura é sempre ROOT/Cidade/Razão Social. O nome da cidade e a razão social são preservados como nomes de pasta, removendo apenas caracteres inválidos no sistema de arquivos. Não são criadas subpastas por competência, tipo de arquivo ou portal. A pasta da empresa contém os arquivos fiscais baixados; resumos de execução ficam separados em `~/.nexfiscal/logs/manifests`. O fluxo guiado preserva os nomes dos arquivos baixados; o armazenamento automático do Livro Fiscal ainda usa nome gerado (limitação registrada na revisão). XML existente **e válido** não é baixado de novo; se estiver corrompido/incompleto, é baixado outra vez. Para bancos antigos, reimporte a planilha com a coluna RAZÃO SOCIAL antes de retomar o processamento.

## Funcionamento da memória dos portais

Com `guided_learning=true`, cada elemento usa seletores já aprendidos. Quando faltam seletores obrigatórios,
o sistema pede orientação ao operador; a rota fiscal inédita permanece manual. A abertura inicial da URL cadastrada
é o comportamento atual, mantido nesta revisão. O registro de ações não reproduz cliques por conta própria.

Somente com `guided_learning=false`, o motor pode procurar novos elementos nesta ordem:

1. estratégia **guardada na memória**; 2. `id`; 3. `name`; 4. `aria-label`; 5. `placeholder`; 6. `label`;
7. texto visível; 8. atributos (title/alt/value); 9. texto vizinho (relação); 10. XPath; 11. CSS;
12. análise da estrutura (ex.: “o campo antes da senha”); 13. OCR (opcional, desligado).

- **Portal novo no modo guiado** → operador orienta o login e realiza as ações fiscais manualmente.
- **Portal conhecido** → carrega o perfil e tenta primeiro o que já sabe, sem reaprender.
- **Portal alterado** → exige orientação no modo guiado; a descoberta automática depende do modo não guiado.

```
PORTAL ALTERADO — https://… | elemento: campo CNPJ/usuário | antes: input[name="usuario"] | agora: input#login | memória atualizada.
```

Também é guardado o fluxo de menus (quais itens clicar) e uma assinatura estrutural da tela de login. A memória fica
no SQLite, separada da automação, e pode ser limpa por portal ou por completo.

### Modo assistido (opcional)

Se um elemento obrigatório não for reconhecido por nenhuma estratégia, em vez de falhar o NexFiscal pode **pedir que você
clique nele** na própria página (faixa azul no topo + aviso no painel; botão *Pular* desiste). O clique é transformado
em estratégia, **gravado na memória do portal** e reutilizado nas próximas execuções — assim cada elemento rebelde é
ensinado uma única vez. Ative em *Configurações → Modo assistido* (`assisted_recognition`); exige navegador visível.
Não é usado para elementos opcionais nem para listas (ex.: links de XML).

## Status, retomada e erros

Estados: `PENDENTE`, `EM_EXECUCAO`, `AGUARDANDO_CAPTCHA`, `CONCLUIDO`, `CONCLUIDO_COM_PENDENCIA`, `ERRO`.

- Uma empresa com problema **não interrompe o lote**.
- **Retomar:** empresas já `CONCLUIDO` na competência são puladas; as demais voltam de onde pararam (etapas já
  concluídas não são refeitas). GUI: *Retomar pendentes* (usa as credenciais armazenadas) · CLI: `python main.py run`
  sem `--planilha`. *Reprocessar já concluídas* / `--forcar` ignora o histórico.
- **Retentativas** (padrão 3): erros temporários (rede, timeout, download) repetem a etapa **sem refazer o CAPTCHA**;
  só sessão expirada exige novo login. Login recusado = `ERRO` sem repetir (evita bloqueio de conta).
- Gravidade: login/consulta falha + Livro falha ⇒ `ERRO`; só uma parte falha, XML com falhas parciais ou Livro
  indisponível ⇒ `CONCLUIDO_COM_PENDENCIA`.
- Mensagens de erro seguem o modelo *Empresa / Cidade / Etapa / Problema / Ação*.

## Relatório

Ao final: totais, XMLs (encontrados/baixados/já existentes/falhas), Livros e quebra por cidade. Exporta para
**XLSX, CSV e PDF** (GUI ou `--exportar arquivo.xlsx`; `python main.py report --competencia 09/2026`).

## Segurança

- Senhas **criptografadas** (Fernet) no SQLite. A chave vem de `NEXFISCAL_KEY`, do cofre do SO (pacote opcional
  `keyring`) ou de `secret.key` na pasta de dados. Para mais proteção use `NEXFISCAL_KEY` ou `keyring`; o arquivo
  de chave ao lado do banco só protege contra leitura casual do banco isolado.
- Senhas registradas são filtradas dos logs e há mascaramento de campos nas capturas opcionais. Dados fiscais visíveis e segredos fora dos padrões conhecidos exigem cuidado ao compartilhar diagnósticos.
- CAPTCHA nunca é salvo. Capturas de tela em erro são **desligadas** por padrão.
- Banco e chave com permissão restrita ao dono (POSIX). XML lido com resolução de entidades e rede desativadas.
- Nenhuma credencial vai para serviço externo.
- Remover senhas: *Configurações → Remover credenciais* ou `python main.py clear-credentials`.
  Limpar memória: *Configurações* ou `python main.py clear-memory`.

Pasta de dados: `~/.nexfiscal` (ou `NEXFISCAL_HOME`) — `nexfiscal.db`, `settings.json`, `logs/`, `tmp/`.

## Configurações

Pasta de saída · competência/datas · intervalo entre operações · tempo máximo de espera · tempo p/ CAPTCHA ·
tentativas · arquivos existentes · navegador (chromium/firefox/webkit) e canal · headless (padrão: **visível**) ·
nível de log · captura de tela em erro · gerenciamento de portais e memória. Valores extras em `settings.json`
(`max_result_pages`, `download_timeout_seconds`, `assist_timeout_seconds`, `database_path`, `enable_ocr`).

## Arquitetura

```
main.py
nexfiscal/
├── config/        settings.py, portal_config.py (vocabulário de reconhecimento)
├── database/      database.py (SQLite), models.py, repositories.py
├── browser/       browser_manager.py, page_manager.py, download_manager.py
├── portal/        portal_registry.py, portal_recognizer.py, portal_memory.py, selector_engine.py,
│                  navigation.py, workflow_engine.py
├── memory/        portal_memory_manager.py
├── authentication/ login.py, captcha.py (só detecta e aguarda)
├── nfse/          nfse_consult.py, xml_downloader.py, xml_validator.py
├── fiscal_book/   fiscal_book.py, downloader.py
├── spreadsheet/   reader.py, validator.py
├── storage/       file_manager.py, organizer.py
├── reports/       report_generator.py, export.py
├── logs/          logger.py
├── ui/            main_window.py, dialogs.py, worker.py
├── controller.py  (lote: cidades → empresas, retomada)   events.py   security.py   dates.py   utils.py
tests/             unitários + ponta a ponta com um portal FALSO local
```


### Adicionar um novo município
Basta cadastrar a URL. Se o portal usar termos diferentes, acrescente-os em `config/portal_config.py`
(listas de palavras-chave) — nenhum código de fluxo precisa mudar.

## Testes

```bash
python -m pip install -r requirements-dev.txt
python -m pip check
python -m ruff check nexfiscal tests --select F
python -m pytest
python -m pip_audit --format json --output dependency-audit.json
```

A suíte cobre regras de aprendizado e CAPTCHA, downloads, organização, reset, persistência, exportação,
falhas de disco/commit, cancelamento e redação dos logs. Inclui servidor HTTP local e cliente HTTP real do Playwright
para validar retry de HTTP 500, encerramento sem retry em HTTP 401 e preservação de XML de consulta sem notas.
Os testes de interação com a página usam objetos simulados; não comprovam navegação ponta a ponta em portais reais.

O build Windows executa testes e auditoria antes de empacotar. O artefato só é publicado após o executável
responder a `--version` com código zero; isso verifica a inicialização da CLI e não substitui teste visual da GUI.

## Diagnóstico e operação

Logs em `NEXFISCAL_HOME/logs` (padrão `~/.nexfiscal/logs`): `nexfiscal.log` legível e `nexfiscal.jsonl`
estruturado. O JSON inclui `run_id`, competência, empresa/cidade durante o processamento, eventos de etapa,
tentativas e erros. Ambos têm rotação de 2 MB por arquivo e cinco cópias. Valores secretos registrados,
campos sensíveis estruturados e query/fragmentos de URLs são filtrados. Restrinja acesso: nomes de empresas,
caminhos e dados operacionais continuam presentes.

Configurações inválidas interrompem a abertura com mensagem e preservam o arquivo. Corrija `settings.json`
antes de reiniciar. O cancelamento é verificado entre ações, buscas e intervalos de retry; uma chamada nativa
de rede/download em andamento pode precisar terminar antes do encerramento. A GUI aguarda o worker para fechar.

Faça backup de banco, chave e configurações com o programa fechado. Uma chave diferente não recupera as senhas
existentes. O reset apaga os cadastros e memórias após confirmação e mantém arquivos fiscais e relatórios.

Comando dedicado para aprendizado manual e comprovante:

```bash
python main.py learn --cidade "Cidade" --url "https://portal.exemplo.gov.br"
```

O comprovante confere a persistência das rotas da sessão e a abertura inicial da URL; marca o teste fiscal
ponta a ponta como `NOT_RUN`. ZIPs aceitos têm até 10.000 entradas, 64 MiB por arquivo e 512 MiB descompactados,
com taxa máxima de compressão de 1.000:1. Exportações maiores devem ser divididas no portal.

Relatório da revisão e decisões pendentes: [docs/revisao-producao.md](docs/revisao-producao.md).

## Limitações conhecidas

- A suíte combina testes unitários e integração HTTP local. **Portais reais variam muito**: espere ajustar o aprendizado na
  primeira execução de cada município e acompanhe esse primeiro lote. Portais que exigem certificado digital,
  seleção de mês/ano por *dropdown*, ou entregam o XML só por e-mail não são cobertos.
- A heurística de “XML já existe” antes do download usa número + data da linha da tabela; se a linha não trouxer
  o número, o arquivo é baixado e depois deduplicado pelo conteúdo.
- OCR é experimental e desligado por padrão.
- Respeite os termos de uso dos portais e a LGPD no tratamento das credenciais de terceiros.
