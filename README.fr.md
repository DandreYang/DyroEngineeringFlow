# DyroEngineeringFlow

[English](README.md) | [简体中文](README.zh-CN.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Português (Brasil)](README.pt-BR.md) | [Русский](README.ru.md)

**DyroEngineeringFlow · `dyro` CLI** est une plateforme local-first d'automatisation d'ingénierie et de contrôle de livraison pour les équipes multi-dépôts. Elle regroupe lignes de développement, Git worktrees, lancement d'agents, portes de qualité, revue indépendante et audit de merge dans une configuration d'espace de travail versionnable.

**Faire avancer l'ingénierie de la tâche jusqu'à la livraison.**

DyroEngineeringFlow n'est lié ni à Codex, ni à Claude, ni à un domaine métier. Chaque équipe fournit un Profile `dyro.toml` pour les dépôts, la disposition, les adaptateurs d'agents et la politique de livraison ; règles métier, coût des modèles et pratiques de release restent dans ce Profile.

## Ce qu'elle impose

- Une tâche appartient à exactement une ligne de développement — jamais un workspace mélangé feature/hotfix.
- Chaque tâche s'exécute dans son propre `git worktree` sur une branche `task/<id>`.
- Les portes sont exécutées par l'orchestrateur ; l'auto-déclaration d'un agent n'est pas une preuve de succès.
- La revue est liée au reçu d'exécution et aux HEAD exacts par dépôt ; une dérive du code l'invalide.
- Une tâche doit avoir une revue indépendante avant `done` ; merge et push exigent une confirmation explicite par défaut.
- Une dépendance terminée ne libère l'aval qu'après intégration de ses HEAD exacts dans la ligne propriétaire.
- La configuration exécutable est représentée en tableaux argv. Le cœur n'exécute jamais de chaînes shell fournies par TOML.

## Démarrage rapide

Pour un usage quotidien de la CLI, installez `dyro` depuis PyPI dans un environnement isolé `pipx` (Python 3.11 ou plus) :

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# Ouvrez un nouveau terminal après ensurepath, puis :
pipx install dyro
dyro --version
```

Pour mettre à jour plus tard : `pipx upgrade dyro`. Si l'équipe gère les paquets Python avec `pip` :

```bash
python3 -m pip install --user --upgrade dyro
```

Placez les dépôts dans un workspace, puis utilisez le parcours d'accueil pour les découvrir, créer les répertoires d'état sûrs et créer la première ligne de développement :

```bash

mkdir my-workspace && cd my-workspace
# Clonez ou déplacez d'abord vos dépôts Git sous ce répertoire.
dyro setup . --name my-workspace --line dev --yes
```

`setup` scanne les dépôts Git locaux, enregistre les chemins relatifs au workspace, dérive les montages de ligne et lit `origin` si disponible — sans éditer le TOML. `--yes` n'est requis que parce que la première ligne crée des worktrees. Utilisez `--no-line` pour le Profile d'abord. Sans dépôts encore, utilisez le parcours guidé :

```bash
dyro init . --wizard --name my-workspace
```

Pour ajouter un dépôt ensuite sans ouvrir `dyro.toml` :

```bash
dyro repo add repositories/services/payments
dyro repo list
```

Politiques et adaptateurs courants :

```bash
dyro config set policy.execution_mode external
dyro config get policy.execution_mode
dyro agent add ci-runner --preset noop
dyro agent test ci-runner
```

Si le Profile contient des remotes, les anchors manquants peuvent être complétés en toute sécurité :

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

Point d'entrée normal d'un nouveau membre :

```bash
dyro start
```

## Flux de livraison

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

Hotfix de production avec base de production vérifiée :

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

Pour un Profile dont l'exécution et l'approbation sont hors machine de confiance locale, configurez `policy.execution_mode = "external"` et `policy.require_external_signoff = true`. Dyro local reste en planification ; après revue liée au reçu et aux HEAD exacts, une signature externe explicite est requise.

```bash
dyro task claim API-101 --by isolated-runner-1
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md --output /runner/out/API-101.zip
dyro task evidence execution API-101 --bundle /runner/out/API-101.zip
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
```

Toute opération d'écriture a un mode planification :

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## Carte des commandes

| Commande | Rôle |
| --- | --- |
| `setup` / `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | Onboarding sans TOML, anchors, choix de ligne et d'agent. |
| `doctor` / `status` | Valider et afficher l'état du plan de contrôle. |
| `line create/list` / `hotfix create` | Créer des lignes feature ou Hotfix depuis une base production explicite. |
| `changeset create/list/verify` | Figer et vérifier les HEAD Git exacts d'une livraison multi-dépôts. |
| `config get/set` / `agent list/add/test` / `open` | Gérer politiques et adaptateurs, tester un exécutable, ouvrir un agent sur la bonne ligne. |
| `task create/list/board/status/next` | Manifestes, état et prochaine tâche exécutable. |
| `task run/answer/gates/review/signoff/merge` | Exécuter, résoudre, portes, revue, signature, fusion. |
| `task claim` / `task evidence build/execution/review` | Claim unique, paquet d'évidence portable pour runner isolé. |
| `task loop/daemon/stats/decisions` | Lots contrôlés, planification, registre et portes de décision. |

## Langues et périmètre actuel

Ce README est maintenu en anglais, chinois simplifié, coréen, espagnol, français, allemand, portugais brésilien et russe. Commandes, clés de configuration, noms de répertoires et règles de sécurité sont identiques dans toutes les traductions. Les messages CLI et guides techniques étendus restent principalement en chinois. Le README multilingue n'implique pas encore de bascule de langue à l'exécution.

DyroEngineeringFlow fournit une boucle locale complète et des contrôles de politique pour les équipes plus strictes en mode planification seule. Il ne crée pas de dépôts distants, n'embarque pas d'identifiants SaaS et ne provisionne pas un runner externe ; il fournit le contrat pour créer et valider des paquets d'évidence portables. Licence [MIT](LICENSE) et paquet [`dyro` sur PyPI](https://pypi.org/project/dyro/).
