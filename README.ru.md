# DyroEngineeringFlow

[English](README.md) | [简体中文](README.zh-CN.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Português (Brasil)](README.pt-BR.md) | [Русский](README.ru.md)

**DyroEngineeringFlow · `dyro` CLI** — local-first платформа автоматизации инженерии и контроля поставки для команд с несколькими репозиториями. Она объединяет линии разработки, Git worktree, запуск агентов, task gates, независимое ревью и аудит merge в версионируемой конфигурации workspace.

**Двигать инженерию от задачи до поставки.**

Не привязана к Codex, Claude или конкретной бизнес-домене. Каждая команда задаёт Profile `dyro.toml` для репозиториев, раскладки, адаптеров агентов и политики поставки; бизнес-правила, стоимость моделей и релизные практики остаются в Profile.

## Что обеспечивается

- Задача принадлежит ровно одной линии разработки — без смешения feature/hotfix workspace.
- Каждая задача выполняется в своём `git worktree` на ветке `task/<id>`.
- Gates выполняет оркестратор; самоотчёт агента не считается доказательством успеха.
- Ревью привязано к execution receipt и точным task HEAD по репозиториям; дрейф кода его инвалидирует.
- Задача становится `done` только после независимого ревью; merge и push по умолчанию требуют явного подтверждения.
- Завершённая зависимость отпускает downstream только после интеграции её точных HEAD в линию-владельца.
- Исполняемая конфигурация — массивы argv. Ядро никогда не выполняет shell-строки из TOML.

## Быстрый старт

Для ежедневной работы с CLI установите `dyro` из PyPI в изолированное окружение `pipx` (Python 3.11+):

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# После ensurepath откройте новый терминал, затем:
pipx install dyro
dyro --version
```

Обновление: `pipx upgrade dyro`. Через `pip`:

```bash
python3 -m pip install --user --upgrade dyro
```

Разместите репозитории в workspace и используйте путь для новичков:

```bash

mkdir my-workspace && cd my-workspace
# Сначала клонируйте или перенесите Git-репозитории в этот каталог.
dyro setup . --name my-workspace --line dev --yes
```

`setup` сканирует локальные Git-репозитории, записывает пути относительно workspace, выводит mounts линии и читает `origin` при наличии — без правки TOML. `--yes` нужен только потому, что первая линия создаёт worktree. `--no-line` — сначала только Profile. Если репозиториев ещё нет:

```bash
dyro init . --wizard --name my-workspace
```

Добавить репозиторий позже без открытия `dyro.toml`:

```bash
dyro repo add repositories/services/payments
dyro repo list
```

Обычные политики и адаптеры:

```bash
dyro config set policy.execution_mode external
dyro config get policy.execution_mode
dyro agent add ci-runner --preset noop
dyro agent test ci-runner
```

Безопасно дополнить недостающие anchors при наличии remotes:

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

Обычная точка входа для нового участника:

```bash
dyro start
```

## Поток поставки

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

Production-hotfix с проверенной production-базой:

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

Если исполнение и утверждение вне локальной границы доверия: `policy.execution_mode = "external"` и `policy.require_external_signoff = true`. Локальный Dyro остаётся в режиме планирования; после ревью, привязанного к receipt и точным HEAD, нужна явная внешняя подпись.

```bash
dyro task claim API-101 --by isolated-runner-1
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md --output /runner/out/API-101.zip
dyro task evidence execution API-101 --bundle /runner/out/API-101.zip
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
```

Все операции записи имеют режим планирования:

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## Карта команд

| Команда | Назначение |
| --- | --- |
| `setup` / `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | Онбординг без TOML, anchors, выбор линии и агента. |
| `doctor` / `status` | Проверить и показать состояние control plane. |
| `line create/list` / `hotfix create` | Feature- или Hotfix-линии от явной production-базы. |
| `changeset create/list/verify` | Зафиксировать и проверить точные clean Git HEAD мульти-репо поставки. |
| `config get/set` / `agent list/add/test` / `open` | Политики и адаптеры, проверка executable, открытие агента на нужной линии. |
| `task create/list/board/status/next` | Манифесты, состояние и следующая исполняемая задача. |
| `task run/answer/gates/review/signoff/merge` | Запуск, ответы, gates, ревью, подпись, merge. |
| `task claim` / `task evidence build/execution/review` | Claim и портативный evidence-пакет для изолированного runner. |
| `task loop/daemon/stats/decisions` | Контролируемые пакеты, планирование, ledger и decision gates. |

## Языки и текущий охват

Этот README поддерживается на английском, упрощённом китайском, корейском, испанском, французском, немецком, бразильском португальском и русском. Команды, ключи конфигурации, имена каталогов и правила безопасности одинаковы во всех переводах. Сообщения CLI и расширенные технические руководства пока в основном на китайском. Многоязычный README ещё не означает переключение языка во время выполнения.

DyroEngineeringFlow даёт полный локальный цикл и policy-контроль для более строгих команд в локальном режиме только планирования. Не создаёт удалённые репозитории, не поставляет SaaS-учётные данные и не разворачивает внешний runner; предоставляет контракт на создание и проверку портативных evidence-пакетов. [Лицензия MIT](LICENSE) и [`dyro` на PyPI](https://pypi.org/project/dyro/).
