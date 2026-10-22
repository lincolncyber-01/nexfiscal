# Aprendizado guiado com URL inicial

O comando `learn` recebe a URL antes de iniciar o navegador, cadastra o portal para a cidade e abre a sessão visível para navegação manual:

```bash
nexfiscal learn --cidade "Minha Cidade" --url "https://portal.exemplo.gov.br"
```

Durante a sessão, o NexFiscal registra apenas metadados estruturados de ações e seletores; valores digitados não são armazenados. O operador navega manualmente e pressiona ENTER no terminal ao concluir. A memória é salva no SQLite por portal/rota.

Ao final, é criado um JSON em `~/.nexfiscal/logs/learning-receipts/` contendo a contagem de eventos/rotas, resultado da releitura do SQLite, hash SHA-256 dos fluxos armazenados e teste de abertura da URL inicial em uma nova sessão.

## O que o comprovante significa

- `persistence_check=PASS`: os eventos foram relidos do SQLite e passaram pela verificação estrutural.
- `initial_url_smoke_test=PASS`: uma nova sessão conseguiu abrir a URL inicial sem resposta HTTP de erro.
- `end_to_end_workflow_test=NOT_RUN`: não significa que o fluxo fiscal completo foi aprovado.

O teste fiscal ponta a ponta deve ser executado separadamente com credenciais e dados de homologação e roteiro de teste aprovado. O comando não reproduz cliques, submete formulários ou dispara downloads automaticamente, pois isso poderia efetuar operações reais no portal. Para um teste ponta a ponta confiável, use uma conta de homologação e confirme que os downloads de XML e Livro Fiscal são válidos.
