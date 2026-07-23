# DyroEngineeringFlow

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md)

**DyroEngineeringFlow · `dyro` CLI** es una plataforma local-first de automatización de ingeniería y control de entrega para equipos con varios repositorios. Unifica líneas de desarrollo, Git worktrees, inicio de agentes, puertas de calidad, revisión independiente y auditoría de merge en una configuración de espacio de trabajo versionable.

**Hace que la ingeniería avance automáticamente desde la tarea hasta la entrega.**

No está acoplada a Codex, Claude ni a un dominio de negocio. Cada equipo define repositorios, estructura, adaptadores de Agent y política de entrega en un Profile `dyro.toml`; las reglas de negocio, el coste del modelo y las prácticas de release permanecen en ese Profile.

## Garantías principales

- Una tarea pertenece a una sola línea de desarrollo; nunca mezcla un workspace de funcionalidad con uno de Hotfix.
- Cada tarea se ejecuta en su propio `git worktree`, en una rama `task/<id>`.
- El orquestador ejecuta las puertas; el informe de un Agent no es evidencia suficiente de éxito.
- La revisión queda vinculada al receipt de ejecución y a los HEAD exactos de cada repositorio; cualquier cambio de código la invalida.
- Una tarea solo llega a `done` tras una revisión independiente; merge y push requieren confirmación explícita de forma predeterminada.
- La configuración ejecutable usa arrays argv. El núcleo nunca ejecuta cadenas shell proporcionadas por TOML.

## Inicio rápido

Para usar la CLI a diario, instala `dyro` desde PyPI en un entorno aislado de `pipx`. Requiere Python 3.11 o posterior.

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# Abre una terminal nueva después de ensurepath y ejecuta:
pipx install dyro
dyro --version
```

Para actualizar después, ejecuta `pipx upgrade dyro`. Si el equipo gestiona los paquetes Python con `pip`, usa:

```bash
python3 -m pip install --user --upgrade dyro
```

Coloca los repositorios en un workspace y luego inicialízalo:

```bash

mkdir my-workspace && cd my-workspace
# Primero clona o mueve los repositorios Git bajo este directorio.
dyro init . --discover --name my-workspace
```

`--discover` explora los repositorios Git locales, registra rutas relativas al workspace, deriva su ubicación en la línea de desarrollo y lee `origin` si está disponible; no hay que editar TOML. Si aún no hay repositorios, usa la alternativa guiada:

```bash
dyro init . --wizard --name my-workspace
```

Para añadir un repositorio después tampoco hay que abrir `dyro.toml`:

```bash
dyro repo add repositories/services/payments
dyro repo list
```

Si el Profile contiene remotes, puede completar de forma segura los repository anchors que falten.

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

El punto de entrada normal de una persona nueva es un comando. Comprueba el workspace y luego permite elegir línea de desarrollo y Agent local.

```bash
dyro start
```

## Flujo de entrega

```bash
dyro doctor
dyro line create release-2026-10 --base origin/main --yes
# Sobrescribe la base verificada solo para los repositorios que lo requieran.
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

Un Hotfix de producción debe indicar una base de producción verificada.

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

Para un Profile cuya ejecución y aprobación se realizan en un sistema de confianza separado, configura `policy.execution_mode = "external"` y `policy.require_external_signoff = true`. Dyro local solo permitirá planificación; incluso tras una revisión vinculada al receipt y a los HEAD exactos de la tarea, se requiere una firma explícita.

```bash
dyro task claim API-101 --by isolated-runner-1
dyro task evidence execution API-101 --receipt /runner/out/receipt.md --gates /runner/out/gates.json --heads /runner/out/task-heads.json
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
```

Todas las operaciones que pueden escribir admiten modo de planificación.

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## Mapa de comandos

| Comando | Propósito |
| --- | --- |
| `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | Onboarding sin editar TOML, gestión de anchors y selección de línea y Agent. |
| `doctor` / `status` | Validar y mostrar el estado del plano de control. |
| `line create/list` / `hotfix create` | Crear líneas de funcionalidad o Hotfix desde una base de producción explícita. |
| `changeset create/list/verify` | Fijar y verificar los HEAD de Git limpios y exactos que componen una entrega multirreposición. |
| `agent list` / `open` | Consultar adaptadores o abrir un Agent en la línea correcta. |
| `task create/list/board/status/next` | Gestionar manifiestos, estado y la siguiente tarea ejecutable. |
| `task run/answer/gates/review/signoff/merge` | Ejecutar, resolver preguntas, aplicar puertas, revisar, firmar y fusionar. |
| `task claim` / `task evidence execution/review` | Reclamo único e importación de pruebas de receipt, puertas, HEAD de tarea y revisión para un runner aislado. |
| `task loop/daemon/stats/decisions` | Lotes controlados, planificación, libro mayor y puertas de decisión. |

## Idiomas y alcance actual

El README se mantiene en inglés, chino simplificado, japonés, coreano y español. Los comandos, claves de configuración, nombres de directorio y reglas de seguridad son iguales en todas las traducciones. Los mensajes de la CLI y las guías técnicas extensas siguen siendo principalmente chinos. El README multilingüe no implica todavía cambio de idioma en tiempo de ejecución.

DyroEngineeringFlow proporciona un ciclo local completo y controles de política para mantener a equipos más estrictos en modo local de solo planificación. No crea repositorios remotos, no incluye credenciales SaaS ni implementa un runner externo; este se integra mediante una extensión de Profile. Se distribuye con la [licencia MIT](LICENSE) y como [`dyro` en PyPI](https://pypi.org/project/dyro/).
