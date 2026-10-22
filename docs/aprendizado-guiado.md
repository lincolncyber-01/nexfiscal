# Aprendizado guiado estrito

O modo `guided_learning` fica habilitado por padrão.

## Regras de execução

- Quando existem seletores aprendidos para o portal, o motor tenta reutilizá-los automaticamente no fluxo fiscal tipado; o registro de eventos por rota é uma trilha de auditoria, não um script de cliques reproduzido literalmente.
- Se um seletor não for encontrado, o motor não tenta inferir outro seletor nem usa OCR; solicita orientação humana quando a etapa permite seleção assistida.
- Para grupos de elementos (por exemplo, uma lista de XMLs), a ausência de seletor aprendido encerra a busca sem descoberta automática.
- Para portal sem seletores essenciais de login aprendidos, o fluxo não abre a URL automaticamente. O operador deve abrir o portal e navegar até a tela de login no navegador visível dentro de `assist_timeout_seconds`.
- No primeiro login guiado, o operador confirma manualmente o envio do formulário; após aprender os seletores de usuário, senha e botão de login, o fluxo tipado pode automatizar logins futuros.
- A detecção e a resolução de CAPTCHA permanecem sob o fluxo manual existente.
- Desative `guided_learning` apenas se quiser explicitamente voltar ao reconhecimento adaptativo automático.

## Configuração

A opção fica em `Settings.guided_learning` e é persistida no arquivo de configurações JSON do NexFiscal. O status do aprendizado guiado é enviado à interface pelo canal de assistência.

## Persistência e reutilização

- Os seletores confirmados são persistidos por portal, etapa e elemento lógico; as execuções futuras tentam esses seletores antes de pedir orientação.
- O gravador registra eventos observados em cada rota e persiste o último conjunto de eventos daquela rota na memória SQLite do portal. As rotas não visitadas continuam preservadas.
- O registro atravessa navegações da mesma sessão por meio de uma ligação direta entre o documento e o processo Python; query strings, fragmentos e credenciais embutidas na URL não são armazenados.
- O registro de eventos é uma trilha estruturada de observação, não um script bruto de replay. O NexFiscal usa seus fluxos fiscais tipados para preencher credenciais, competências, executar downloads e validar arquivos. Reproduzir cliques cegamente seria inseguro (poderia submeter formulários ou repetir downloads) e os valores de campos são intencionalmente omitidos.
- Se um seletor salvo falhar, o motor não tenta descobrir substitutos automaticamente no modo guiado. A etapa falha de forma controlada ou solicita seleção assistida conforme a configuração; a execução não deve ser considerada concluída só porque existe um registro de eventos.

A tabela portal_flows armazena os eventos como strings JSON, preservando a compatibilidade com a estrutura SQLite existente.
A opção Settings.guided_learning controla esse comportamento e permanece habilitada por padrão.

## Registro estruturado de ações guiadas

O módulo `nexfiscal.portal.guided_action_recorder.GuidedActionRecorder` captura cliques e
mudanças de campos na página atual e em documentos seguintes. Cada evento inclui ordem,
URL sem query/fragmento, seletor candidato, tipo do controle e rótulo. **Valores digitados
não são registrados**; eventos de entrada em campos sensíveis são ignorados. Isso evita
persistir senhas, tokens ou dados de formulário acidentalmente.

Os eventos são coletados durante a execução do CompanyWorkflow e persistidos ao final,
inclusive em falhas e cancelamentos, separados por rota e vinculados ao perfil do portal.
A tabela portal_flows armazena os eventos como strings JSON.

O registro não é reproduzido como uma sequência cega de cliques. O NexFiscal usa os
seletores aprendidos para executar o fluxo fiscal tipado, preenchendo credenciais e
competências com os dados da execução, validando downloads e tratando CAPTCHA no fluxo
manual existente. Reexecutar cliques gravados cegamente poderia submeter formulários ou
duplicar operações; por isso, eventos são trilha estruturada, enquanto seletores confirmados
são o mecanismo de reutilização automática.
