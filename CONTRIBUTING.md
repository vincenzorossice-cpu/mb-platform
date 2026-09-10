# CONTRIBUTING — MB Platform Documentation

```yaml
---
id: CONTRIBUTING
title: "CONTRIBUTING — Come contribuire alla documentazione MB Platform"
status: active
created: 2026-07-23
updated: 2026-07-23
---
```

## Workflow di contribuzione

### 1. Struttura dei documenti

Ogni documento `.md` deve avere:
- **Frontmatter YAML** con campi obbligatori: `id`, `title`, `status`, `created`, `updated`
- Campi opzionali: `sources`, `kb_links`, `depends_on`, `consumed_by`, `related`, `parent`
- **Contenuto** in markdown semantico (headings gerarchici, tabelle, code blocks)

### 2. Convenzioni

| Convenzione | Regola |
|---|---|
| ID documento | `UPPER-CASE-WITH-DASHES` (es. `ARCH-OVERVIEW`) |
| Error codes | `UPPER_SNAKE_CASE` (es. `DEVICE_OFFLINE`, `OCR_LOW_CONFIDENCE`) |
| Naming file | `{percorso}/{nome}.md` (es. `docs/modules/signal-parser/README.md`) |
| Status | `active` \| `draft` \| `deprecated` |
| Superseded | Usare `supersedes` e `superseded_by` nel frontmatter |

### 3. Moduli (Agent README pattern)

Per ogni nuovo modulo, documentare:
1. **Responsabilità** (cosa fa)
2. **Confini** (cosa NON fa)
3. **Interfaccia** (metodi Python con type hints)
4. **Output contracts** (dataclass)
5. **Error codes** (tabella: codice | quando | cosa fa l'orchestratore)
6. **Fallback logic** (cosa fa quando il percorso primario fallisce)
7. **HITL triggers** (quando richiede intervento umano)
8. **Retry policy** (max retries, backoff, retry on / non retry on)
9. **Metriche** (metrica | misurazione | target)
10. **KB tracciabilità** (guide P1-P8 referenziate)

### 4. Flussi operativi

Per ogni flusso:
1. **ID Flusso** (es. A1, B3)
2. **Attore** e **Frequenza**
3. **Precondizioni** e **Postcondizioni**
4. **Step-by-step** (numerati, con System Action e UI State)
5. **Errori e Edge Cases**

### 5. CI/CD (raccomandato)

```yaml
# .github/workflows/docs-lint.yml
name: Docs Lint
on: [push, pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint markdown
        run: npx markdownlint-cli2 'docs/**/*.md'
      - name: Check frontmatter
        run: python scripts/validate_frontmatter.py docs/
      - name: Link check
        run: npx markdown-link-check docs/**/*.md
```

### 6. Versionamento

- Semantic versioning per la documentazione (v2.0, v2.1, ecc.)
- Ogni revisione aggiorna `updated` nel frontmatter
- CHANGELOG registra tutte le modifiche
- Documenti deprecati: `status: deprecated` + `superseded_by: nuovo-id`
