# Revisão de robustez do NexFiscal

Revisão de 29/10/2026 sobre `main` em `00f6a80f61bf0cbdf1575da96d550cc073b572cf`.
O escopo inclui código ativo em `nexfiscal/`, pontos de entrada, módulos legados na raiz,
testes, dependências e workflows. As correções foram apresentadas por problema, impacto e solução
antes da implementação. Esta revisão não certifica homologação em portais municipais reais.

## Problemas priorizados e soluções aplicadas

| Risco | Âmbito | Problema e impacto | Solução aplicada / verificação |
|---|---|---|---|
| Alto | Arquitetura / CLI | `learn` era aceito pelo parser, mas caía em `cmd_maint`, apagando memória. | Despacho dedicado para `run_guided_learning`; manutenção aceita apenas comandos explícitos. Teste com SQLite confirma preservação da memória anterior. |
| Alto | Integridade / erros | Falha no manifesto marcava a etapa como erro, mas a empresa terminava concluída. | Resultado final passa a `CONCLUIDO_COM_PENDENCIA` e registra a falha. Teste injeta erro de disco. |
| Alto | Persistência | Erro no `COMMIT` não passava pelo rollback; importação de memória podia ficar parcial. | Commit dentro do tratamento transacional, rollback explícito, savepoints para transações aninhadas; importação valida o documento e grava tudo em uma transação. Testes usam FK diferida e erro no segundo portal. |
| Alto | Configuração | JSON inválido ou configuração ilegível retornava padrões sem informar o usuário. | Erro de configuração explícito, arquivo preservado, mensagens na CLI e GUI; testes para JSON truncado, valores inválidos e UTF-8 inválido. |
| Alto | Segurança / arquivos | ZIP sem limites permitia consumo excessivo de RAM, CPU e disco; nomes com barras invertidas variavam por plataforma. | Pré-validação de quantidade, tamanho e compressão compartilhada entre extração e Livro Fiscal; nomes reduzidos a componentes seguros. Testes verificam recusa antes da extração e contenção de caminhos. |
| Alto | Segurança / relatórios | XLSX interpretava texto externo iniciado por `=` como fórmula. | Células textuais explicitamente armazenadas como texto, inclusive resumo/cidades, preservando células numéricas. CSV mantém sua proteção. |
| Médio | Qualidade / arquitetura | Configurações, relatórios, comprovantes e manifestos tinham estratégias distintas de gravação. | `storage.atomic` centraliza temporário no mesmo diretório, `fsync`, publicação por `os.replace` e limpeza com diagnóstico. Falha de serialização preserva o relatório anterior. Não equivale a uma transação distribuída ou garantia contra falha elétrica. |
| Médio | Automação | Cancelamento não era consultado durante buscas/retries; contextos podiam vazar ao falhar a criação de página. | Checkpoints nas buscas, espera de retry interrompível, fechamento do contexto em erro, startup idempotente e erros de encerramento registrados. |
| Médio | GUI / ciclo de vida | Janela aceitava fechamento após esperar 15 s mesmo com worker ativo. | Fechamento adiado até término seguro; teste confirma cancelamento solicitado e evento de fechamento ignorado. Erros de exportação, configuração e retomada são apresentados. |
| Médio | Observabilidade | Campos enviados por `extra` eram descartados pelo formato textual; reconfiguração não fechava handlers. | `nexfiscal.jsonl` rotativo mantém campos estruturados, contexto de lote/empresa e traceback filtrado; logs textuais permanecem. Handlers substituídos são fechados. |
| Médio | Segurança / diagnóstico | Redação simples não cobria valores estruturados e parâmetros arbitrários de URL. | Filtro de campos sensíveis aninhados, valores secretos registrados, URLs sem query/fragmento/credenciais e exceções. Erros de lote são diagnosticados antes de limpar o registro de segredos e retornam mensagem sanitizada. |
| Médio | Performance | PDF inteiro era carregado apenas para conferir `%%EOF`; corpos HTTP ficavam retidos no contexto Playwright. | Leitura limitada aos 2 KiB finais do PDF; descarte explícito das respostas HTTP após copiar os bytes. |
| Médio | Aprendizado | Comprovante contava eventos de rotas de sessões anteriores e podia reprovar persistência correta. | Comparação exata dos fluxos da sessão corrente, sem apagar os anteriores; comprovantes atômicos com nomes únicos. Teste com rota preexistente. |
| Médio | Dependências / CI | Intervalos abertos variavam o ambiente; build publicava ZIP antes de exigir diagnóstico bem-sucedido. | Snapshot `constraints.txt`, ferramentas separadas para desenvolvimento/build, testes Linux/Windows, auditoria de pacotes, lint e manifesto de versões. Build exige testes, auditoria e `--version` com saída zero antes de publicar o artefato. |
| Médio | Documentação | README descrevia descoberta automática no modo guiado e testes ponta a ponta inexistentes. | Instruções alinhadas ao código ativo, regra de acesso manual explícita, comandos válidos, logs e limites documentados; limitações de teste declaradas. |

## Regras preservadas

- Após qualquer CAPTCHA observado, o operador continua responsável por clicar em **Entrar/Acessar**.
  Não foi alterado `authentication/login.py` nem introduzido clique, Enter, submissão via JS ou replay alternativo.
  Testes percorrem nova tentativa de login com e sem modo guiado e confirmam ausência de envio automático.
- Estrutura fiscal continua `ROOT/Cidade/Razão Social`, sem subpastas por competência.
- XML de consulta sem notas continua sendo salvo e validado; o teste HTTP real cobre esse caso.
- Primeiro acesso continua usando o comportamento existente: abrir a URL cadastrada, orientação humana e
  ações fiscais manuais quando a rota não está aprendida. A revisão não amplia descoberta/replay autônomo.
- Reset mantém confirmação explícita e preservação dos arquivos fiscais e relatórios existentes.

## Validação e alcance

Comandos reproduzíveis com Python 3.12:

```bash
python -m pip install --upgrade pip==26.2.2
python -m pip install -r requirements-build.txt
python -m pip check
python -m ruff check nexfiscal tests --select F
python -m pytest --junitxml=test-results.xml
python -m pip_audit --format json --output dependency-audit.json
```

A suíte original tinha 76 testes. Os novos testes cobrem falhas de commit/disco, importação parcial,
configuração corrompida, regressão destrutiva de `learn`, ZIPs excessivos, fórmulas em XLSX,
contexto/redação de logs, cancelamento, recursos de navegador, fechamento da GUI e CAPTCHA em retentativas.
A integração HTTP usa servidor local e o cliente real do Playwright, com HTTP 500, HTTP 401 e XML sem notas.

Resultado local final: **107 testes passaram**, lint sem achados, `pip check` sem incompatibilidades e
**63 pacotes auditados, zero alertas e nenhum pacote ignorado**, após atualizar o pip. Os comandos
`main.py --version` e `main.py portal list` também foram executados em uma pasta de dados de teste isolada.

A primeira execução da CI detectou ausência de `libEGL.so.1` no runner Linux ao importar Qt.
O workflow instala `libegl1`/`libopengl0` explicitamente e usa a plataforma Qt `offscreen` para os testes.

O download local do Chromium falhou com ZIP vazio/truncado. Por isso não se declara teste visual do navegador
ou homologação fiscal ponta a ponta. O novo build Windows na CI exige validação própria; `--version` apenas
prova inicialização da CLI. A interface, o navegador visível e o processo fiscal real ainda precisam de homologação.

A auditoria das dependências da aplicação não apontou vulnerabilidades conhecidas na consulta realizada.
Ao incluir ferramentas de desenvolvimento/build, foram encontrados alertas no `pip` 25.0.1 fornecido pelo
ambiente; a revisão atualiza essa ferramenta para 26.2.2 e exige a mesma versão na CI. O relatório de auditoria
e as versões resolvidas são publicados como artefatos de validação. Uma auditoria sem alertas não detecta
vulnerabilidades ainda desconhecidas, código malicioso em pacotes ou problemas do próprio sistema operacional.
Referência da ferramenta: [PyPA pip-audit](https://github.com/pypa/pip-audit).

## Decisões críticas pendentes de confirmação

Estes comportamentos foram identificados, mas não alterados porque a solicitação exige confirmação para mudanças críticas:

| Área / código | Risco remanescente | Proposta para aprovação e validação específica |
|---|---|---|
| Evidências de arquivos — `workflow_engine._reconcile_guided_folder` | XML/Livro Fiscal antigos na mesma pasta podem ser considerados evidência da execução atual; PDFs com nomes genéricos também podem ser classificados por contexto. | Associar arquivos ao lote, empresa e competência por manifesto/hash e exigir confirmação quando o conteúdo não comprovar a competência; manter a estrutura de pastas atual. |
| Autenticação — `LoginHandler.is_logged_in`, `_wait_outcome`, `PageManager.fill` | Mudança de URL sem campo de senha pode ser aceita como login; término da espera sem campo de senha retorna sucesso; falha de leitura de campo é tratada como preenchimento aceito. | Usar marcador autenticado aprendido por portal e verificação explícita do preenchimento, com pendência quando não houver evidência suficiente. Preservar sempre o clique humano após CAPTCHA. |
| Credenciais de planilha — `spreadsheet.reader._clean_cell` | Limpeza genérica remove espaços e transforma alguns textos, inclusive da senha. | Separar a leitura de senha dos outros campos e preservar o conteúdo exato; requer teste com planilhas reais e senhas afetadas. |
| Nomes do Livro Fiscal — `LivroFileStore.store` | O caminho automático ainda usa nome sintético; o caminho guiado preserva o nome do portal. | Unificar preservação de nome com política explícita de colisão, após confirmar compatibilidade com reconhecimento de arquivos já existentes. |
| Chave de criptografia — `security._load_or_create_key` | Chave local fica próxima ao banco; adicionar/remover backend keyring pode selecionar uma chave diferente. | Migração explícita com backup e verificação, sem gerar/substituir chave de uma instalação existente silenciosamente. Windows exige proteção de ACL/cofre do sistema. |
| Retentativas de ações de UI | Uma operação que expirou pode já ter sido recebida pelo portal; repetir a etapa pode repetir exportações. | Usar confirmação de resultado por portal antes de replay de operações com efeitos, com testes de homologação. |

## Outros riscos operacionais

- Arquivos fiscais e logs contêm dados pessoais/empresariais; definir acesso, retenção, backups e restauração.
  Capturas de tela são opcionais e podem conter dados fiscais visíveis fora dos campos de formulário.
- Uma chamada nativa Playwright de download/rede pode atrasar o cancelamento; o fechamento seguro aguarda seu retorno.
- Limites de ZIP são conservadores e podem recusar exportações legítimas muito grandes; dividir a exportação
  ou revisar limites com dados representativos. A validação é estrutural, sem validação fiscal por XSD de cada município.
- As gravações atômicas e o reset com compensação não constituem transação entre SQLite e sistema de arquivos;
  queda de energia entre os dois exige reconciliação. Testar restauração de backup com o programa fechado.
- Permanecem cópias legadas na raiz (`cli.py`, `controller.py`, `authentication/`, `ui/`, `portal/`).
  `main.py` e o empacotamento usam `nexfiscal/`; consolidar/remover as variantes exige verificar se algum usuário
  ainda depende delas. Novas alterações devem ir para o pacote ativo.
- Executáveis Windows ainda precisam de assinatura e reputação de distribuição; esta revisão não desativa
  proteções do Chrome/Windows e não garante ausência de alertas de download.
- Não foi fixada uma meta percentual de cobertura: os testes exercitam regressões concretas, não provam
  que todos os caminhos ou portais estão cobertos. Homologar ao menos um lote real supervisionado, uma
  competência sem notas, expiração de sessão, indisponibilidade de rede e retomada com arquivos preexistentes.
