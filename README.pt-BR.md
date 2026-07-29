# DyroEngineeringFlow

[English](README.md) | [简体中文](README.zh-CN.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Português (Brasil)](README.pt-BR.md) | [Русский](README.ru.md)

**DyroEngineeringFlow · `dyro` CLI** é uma plataforma local-first de automação de engenharia e controle de entrega para equipes multi-repositório. Une linhas de desenvolvimento, Git worktrees, lançamento de agents, gates de tarefa, revisão independente e auditoria de merge em uma configuração de workspace versionável.

**Manter a engenharia avançando da tarefa até a entrega.**

Não está acoplada a Codex, Claude ou a um domínio de negócio. Cada equipe fornece um Profile `dyro.toml` para repositórios, layout, adaptadores de agent e política de entrega; regras de negócio, custo de modelo e práticas de release ficam no Profile.

## O que ela impõe

- Uma tarefa pertence a exatamente uma linha de desenvolvimento — nunca um workspace misto de feature/hotfix.
- Cada tarefa roda no próprio `git worktree` em um branch `task/<id>`.
- Gates são executados pelo orquestrador; o auto-relato de um agent não é evidência de sucesso.
- A revisão fica ligada ao receipt de execução e aos HEADs exatos por repositório; deriva de código a invalida.
- Uma tarefa precisa de revisão independente antes de `done`; merge e push exigem confirmação explícita por padrão.
- Uma dependência concluída só libera o fluxo a jusante após integrar seus HEADs exatos na linha dona.
- Configuração executável usa arrays argv. O core nunca executa strings shell vindas do TOML.

## Início rápido

Para uso diário da CLI, instale `dyro` do PyPI em um ambiente isolado `pipx` (Python 3.11+):

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# Abra um novo terminal após ensurepath e então:
pipx install dyro
dyro --version
```

Para atualizar depois: `pipx upgrade dyro`. Com `pip`:

```bash
python3 -m pip install --user --upgrade dyro
```

Coloque os repositórios em um workspace e use o caminho de onboarding:

```bash

mkdir my-workspace && cd my-workspace
# Clone ou mova primeiro os repositórios Git sob este diretório.
dyro setup . --name my-workspace --line dev --yes
```

`setup` varre repositórios Git locais, grava caminhos relativos ao workspace, deriva montagens da linha e lê `origin` quando disponível — sem editar TOML. `--yes` só é necessário porque a primeira linha cria worktrees. Use `--no-line` se quiser o Profile primeiro. Sem repositórios ainda:

```bash
dyro init . --wizard --name my-workspace
```

Adicionar repositório depois sem abrir `dyro.toml`:

```bash
dyro repo add repositories/services/payments
dyro repo list
```

Políticas e adaptadores comuns:

```bash
dyro config set policy.execution_mode external
dyro config get policy.execution_mode
dyro agent add ci-runner --preset noop
dyro agent test ci-runner
```

Completar anchors em falta com remotes:

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

Entrada normal de um novo membro:

```bash
dyro start
```

## Fluxo de entrega

```bash
dyro doctor
dyro line create release-2026-10 --base origin/main --yes
dyro line create release-2026-10 --base origin/main --repo-base web=v2026.10.0 --yes
dyro open release-2026-10 --agent codex
dyro task create API-101 --title "Implement API contract" --line release-2026-10 --repository api
dyro task next
dyro task next --run --yes
dyro task review API-101
dyro task merge API-101 --yes
dyro changeset create release-2026-10-ready --line release-2026-10
dyro changeset verify release-2026-10-ready
```

Hotfix de produção com base de produção verificada:

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

Quando execução e aprovação ficam fora da confiança local: `policy.execution_mode = "external"` e `policy.require_external_signoff = true`. O Dyro local permanece em planejamento; após revisão ligada ao receipt e aos HEADs, é necessária assinatura externa explícita.

```bash
dyro task claim API-101 --by isolated-runner-1
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md --output /runner/out/API-101.zip
dyro task evidence execution API-101 --bundle /runner/out/API-101.zip
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
```

Toda operação de escrita tem modo de planejamento:

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## Mapa de comandos

| Comando | Propósito |
| --- | --- |
| `setup` / `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | Onboarding sem TOML, anchors, escolha de linha e agent. |
| `doctor` / `status` | Validar e exibir o estado do plano de controle. |
| `line create/list` / `hotfix create` | Criar linhas de feature ou Hotfix a partir de base de produção explícita. |
| `changeset create/list/verify` | Fixar e verificar HEADs Git limpos e exatos de uma entrega multi-repo. |
| `config get/set` / `agent list/add/test` / `open` | Políticas e adaptadores, testar executável, abrir agent na linha certa. |
| `task create/list/board/status/next` | Manifestos, estado e próxima tarefa executável. |
| `task run/answer/gates/review/signoff/merge` | Executar, resolver, gates, revisão, assinatura e merge. |
| `task claim` / `task evidence build/execution/review` | Claim único e pacote de evidência portátil para runner isolado. |
| `task loop/daemon/stats/decisions` | Lotes controlados, planejamento, livro-razão e portas de decisão. |

## Idiomas e escopo atual

Este README é mantido em inglês, chinês simplificado, coreano, espanhol, francês, alemão, português do Brasil e russo. Comandos, chaves de configuração, nomes de diretório e regras de segurança são idênticos em todas as traduções. Mensagens da CLI e guias técnicos extensos continuam principalmente em chinês. README multilíngue ainda não implica troca de idioma em tempo de execução.

DyroEngineeringFlow oferece um ciclo local completo e controles de política para equipes mais rígidas em modo local só de planejamento. Não cria repositórios remotos, não embute credenciais SaaS e não provisiona runner externo; oferece o contrato para criar e validar pacotes de evidência portáteis. [Licença MIT](LICENSE) e [`dyro` no PyPI](https://pypi.org/project/dyro/).
