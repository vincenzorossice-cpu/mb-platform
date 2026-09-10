# Export Manifest — MB Platform Documentation → Git Repository

```yaml
---
id: EXPORT-MANIFEST
title: "Export Manifest — Mappatura Note → Path Git Repository"
status: active
created: 2026-07-23
updated: 2026-07-23
version: "v2.0"
total_documents: 27
total_notes: 27
---
```

## Istruzioni per l'export

1. Creare il repository git: `mkdir mb-platform && cd mb-platform && git init`
2. Creare la struttura di directory: `mkdir -p docs/architecture docs/adr docs/contracts docs/modules/signal-parser docs/flows docs/hitl docs/kpi docs/risk docs/roadmap docs/process-resolution ops/runbooks .github/workflows`
3. Per ogni riga della tabella qui sotto, recuperare il contenuto della note (tramite API o copia manuale) e salvarlo nel path git indicato
4. Aggiungere i file di CI/CD (vedi sezione workflow)
5. `git add . && git commit -m "v2.0 — Engineering-ready documentation (agentic-first)"`

## Mappatura Note → Path Git (27 documenti)

### Root files (3)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 1 | `41b38721` | `README.md` | README |
| 2 | `4d5e9cd6` | `CHANGELOG.md` | CHANGELOG |
| 3 | `ae4b5380` | `CONTRIBUTING.md` | CONTRIBUTING |

### docs/architecture/ (5)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 4 | `3dcf6092` | `docs/architecture/overview.md` | ARCH-OVERVIEW |
| 5 | `0b1e0442` | `docs/architecture/data-model.md` | ARCH-DATA-MODEL |
| 6 | `efb683fc` | `docs/architecture/technical.md` | ARCH-TECHNICAL |
| 7 | `a2b583e8` | `docs/architecture/deployment.md` | ARCH-DEPLOYMENT |
| 8 | `9e4530d3` | `docs/architecture/testing-security-openitems.md` | ARCH-TEST-SEC-OPEN |

### docs/adr/ (2)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 9 | `dd010310` | `docs/adr/0001-agentic-architecture.md` | ADR-0001 |
| 10 | `e7cf88d2` | `docs/adr/0002-screen-first.md` | ADR-0002 |

### docs/contracts/ (1)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 11 | `e52b1efa` | `docs/contracts/README.md` | CONTRACTS-ALL |

### docs/modules/ (4)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 12 | `26d8ce3f` | `docs/modules/signal-parser/README.md` | MOD-SIGNAL-PARSER |
| 13 | `f59db254` | `docs/modules/signal-parser/design.md` | MOD-SIGNAL-PARSER-DESIGN |
| 14 | `1e90c0e4` | `docs/modules/error-handling-system.md` | SYS-ERROR-HANDLING |
| 15 | `46ec67ae` | `docs/modules/core-agents.md` | MOD-CORE-AGENTS |

### docs/flows/ (5)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 16 | `abc5b492` | `docs/flows/use-case-map.md` | FLOWS-USE-CASE-MAP |
| 17 | `c91f758c` | `docs/flows/A-daily-operations.md` | FLOW-A-DAILY-OPS |
| 18 | `799e3161` | `docs/flows/B-onboarding-setup.md` | FLOW-B-ONBOARDING |
| 19 | `5674a41f` | `docs/flows/CDEF-bookmaker-devices-credentials-kb.md` | FLOWS-CDEF |
| 20 | `e7a73923` | `docs/flows/GH-monitoring-reconciliation.md` | FLOWS-GH-MONITORING |

### docs/hitl/ (2)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 21 | `91327a35` | `docs/hitl/dashboard-design.md` | HITL-DASHBOARD |
| 22 | `d74ac12d` | `docs/hitl/design-system.md` | HITL-DESIGN-SYSTEM |

### docs/process-resolution/ (1)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 23 | `4649ea4a` | `docs/process-resolution/README.md` | PROC-RESOLUTION |

### docs/kpi/ (1)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 24 | `a3d65403` | `docs/kpi/README.md` | KPI-DOD |

### docs/risk/ (1)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 25 | `a600955d` | `docs/risk/register.md` | RISK-REGISTER |

### docs/roadmap/ (1)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 26 | `099685fe` | `docs/roadmap/README.md` | ROADMAP |

### ops/runbooks/ (1)

| # | Note ID | Git Path | Doc ID |
|---|---|---|---|
| 27 | `e702ec24` | `ops/runbooks/incident-response.md` | OPS-OBSERVABILITY |

---

## Struttura directory completa

```
mb-platform/
├── README.md                                    # 41b38721
├── CHANGELOG.md                                 # 4d5e9cd6
├── CONTRIBUTING.md                              # ae4b5380
├── .github/
│   └── workflows/
│       └── docs-lint.yml                        # (da creare — vedi CONTRIBUTING.md)
├── docs/
│   ├── architecture/
│   │   ├── overview.md                          # 3dcf6092
│   │   ├── data-model.md                        # 0b1e0442
│   │   ├── technical.md                         # efb683fc
│   │   ├── deployment.md                        # a2b583e8
│   │   └── testing-security-openitems.md        # 9e4530d3
│   ├── adr/
│   │   ├── 0001-agentic-architecture.md         # dd010310
│   │   └── 0002-screen-first.md                 # e7cf88d2
│   ├── contracts/
│   │   └── README.md                            # e52b1efa
│   ├── modules/
│   │   ├── signal-parser/
│   │   │   ├── README.md                        # 26d8ce3f
│   │   │   └── design.md                        # f59db254
│   │   ├── error-handling-system.md             # 1e90c0e4
│   │   └── core-agents.md                       # 46ec67ae
│   ├── flows/
│   │   ├── use-case-map.md                      # abc5b492
│   │   ├── A-daily-operations.md                # c91f758c
│   │   ├── B-onboarding-setup.md                # 799e3161
│   │   ├── CDEF-bookmaker-devices-credentials-kb.md # 5674a41f
│   │   └── GH-monitoring-reconciliation.md      # e7a73923
│   ├── hitl/
│   │   ├── dashboard-design.md                  # 91327a35
│   │   └── design-system.md                     # d74ac12d
│   ├── kpi/
│   │   └── README.md                            # a3d65403
│   ├── risk/
│   │   └── register.md                          # a600955d
│   ├── roadmap/
│   │   └── README.md                            # 099685fe
│   └── process-resolution/
│       └── README.md                            # 4649ea4a
└── ops/
    └── runbooks/
        └── incident-response.md                 # e702ec24
```

## Script di export (bash)

```bash
#!/bin/bash
# export-to-git.sh — Esporta le 27 note nel repository git
set -e

REPO_DIR="${1:-mb-platform}"

mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init

# Struttura directory
mkdir -p .github/workflows
mkdir -p docs/{architecture,adr,contracts,modules/signal-parser,flows,hitl,kpi,risk,roadmap,process-resolution}
mkdir -p ops/runbooks

# TODO: Per ogni note ID nella tabella sopra, recuperare il contenuto
# via API e salvarlo nel path git corrispondente.
# 
# Esempio (pseudocodice):
# fetch_note_content 41b38721 > README.md
# fetch_note_content 4d5e9cd6 > CHANGELOG.md
# fetch_note_content ae4b5380 > CONTRIBUTING.md
# fetch_note_content 3dcf6092 > docs/architecture/overview.md
# ... ecc per tutti i 27 documenti

git add .
git commit -m "v2.0 — Engineering-ready documentation (agentic-first, screen-first)

24 documents rewritten from 22 source chapters (40 KB MB PRD files).
- 2 ADR (agentic-first, screen-first)
- 10 modules with Agent README + contracts + error codes
- 48 use cases in 8 categories (A-H)
- 11 data model entities
- 3-level failure model with checkpoints
- 9 system KPI + per-process KPI + DoD
- 19 risks (8 tech + 6 ops + 5 business)
- 17 open items with owner and deadline
- 4 runbooks
- HITL Dashboard Mission Control v2
- 10 User Stories in 4 Epics
- KB tracciabilità: 140 guides (P1-P8)

Co-authored-by: Claude (Anthropic)
"

echo "Export complete. $REPO_DIR ready."
```

## Verifica post-export

Dopo l'export, verificare:
1. `find . -name "*.md" | wc -l` → deve restituire 27
2. `grep -r "id: " docs/ | wc -l` → deve restituire 24 (frontmatter ID nei documenti docs/)
3. `grep -r "ADR-000" docs/ | wc -l` → verificare che ADR-0001 e ADR-0002 esistano
4. `grep -r "48 casi" docs/` → verificare che il conteggio use case sia corretto
5. `grep -r "Modules (4)" README.md` → verificare che il count sia corretto
6. Link check: `npx markdown-link-check docs/**/*.md`
7. Frontmatter validation: `python scripts/validate_frontmatter.py docs/`
