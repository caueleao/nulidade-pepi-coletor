# nulidade-pepi-coletor

Coletor distribuído das peças de processos administrativos de nulidade de patente no pePI
(INPI), para um estudo empírico sobre nulidade de patentes de IA no Brasil.

Cada shard do workflow `coleta.yml` roda num runner do GitHub Actions, com IP próprio, e
visita uma fatia da fila: baixa os despachos do caso (tabela Publicações) e as petições das
partes (tabela Serviços). A fila vem do Cloudflare R2 e os PDFs voltam para lá; nenhum dado
passa por este repositório. Credenciais ficam nos secrets do repositório.

- `scripts/baixar_pecas.py` — visita os processos e baixa as peças (`--shard K --total-shards N`)
- `scripts/r2.py` — baixa a fila e sincroniza PDFs e a base do shard com o R2
- `scripts/inpi_browser.py`, `captcha_solver.py`, `config.py` — sessão Playwright no pePI e
  solução de reCAPTCHA v2

Os documentos são públicos no pePI; a consulta às petições exige aceitar a Declaração de
Finalidade, o que o coletor faz em cada processo.
