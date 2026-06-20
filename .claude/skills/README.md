# Ponytail skills

These are the [ponytail](https://github.com/DietrichGebert/ponytail) agent
skills — a minimalist-code ruleset ("the best code is the code you never
wrote"). They are vendored here as Claude Code project skills, so each is
invocable as a slash command in this repo.

| Skill | Invoke | What it does |
|-------|--------|--------------|
| `ponytail` | `/ponytail [lite\|full\|ultra]` | Lazy mode: the simplest solution that works (YAGNI → stdlib → native → one line). |
| `ponytail-review` | `/ponytail-review` | Reviews a diff for over-engineering — what to delete. |
| `ponytail-audit` | `/ponytail-audit` | Whole-repo scan for bloat to delete/simplify. |
| `ponytail-debt` | `/ponytail-debt` | Harvests `ponytail:` shortcut comments into a debt ledger. |
| `ponytail-gain` | `/ponytail-gain` | Shows ponytail's benchmark scoreboard. |
| `ponytail-help` | `/ponytail-help` | Reference card for all ponytail modes and skills. |

## Provenance

Copied verbatim from [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)
(`skills/`), MIT licensed — see [`LICENSE`](LICENSE). Upstream carries more
(hooks, plugin configs, benchmarks); only the skills are vendored here.
