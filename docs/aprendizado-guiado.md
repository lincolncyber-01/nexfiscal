# Aprendizado guiado estrito

O modo `guided_learning` fica habilitado por padrão.

## Regras de execução

- Quando existe roteiro salvo, o motor tenta usar apenas os seletores persistidos.
- Se um seletor não for encontrado, o motor não tenta inferir outro seletor nem usa OCR; solicita orientação humana quando a etapa permite seleção assistida.
- Para grupos de elementos (por exemplo, uma lista de XMLs), a ausência de seletor aprendido encerra a busca sem descoberta automática.
- Para portal sem seletores aprendidos, o fluxo não abre a URL automaticamente. O operador deve abrir o portal no navegador visível dentro de `assist_timeout_seconds`.
- A detecção e a resolução de CAPTCHA permanecem sob o fluxo manual existente.
- Desative `guided_learning` apenas se quiser explicitamente voltar ao reconhecimento adaptativo automático.

## Configuração

A opção fica em `Settings.guided_learning` e é persistida no arquivo de configurações JSON do NexFiscal. O status do aprendizado guiado é enviado à interface pelo canal de assistência.

## Limitações a validar

O modo impede descoberta automática de seletores; ele não grava por si só cada clique como um evento de baixo nível. A memória atual persiste estratégias de seletores por etapa e chave. A captura completa de sequências de ações (tipo de evento, ordem e parâmetros não sensíveis) deve ser tratada como uma etapa adicional antes de considerar o requisito de gravação de roteiro integralmente atendido.
