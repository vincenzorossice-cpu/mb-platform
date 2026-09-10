# Deployment & Infrastructure

```yaml
---
id: ARCH-DEPLOYMENT
title: "Infrastruttura, Hardware, Costi e Dimensionamento"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 16 (Infrastruttura e costi)
related_adrs:
  - ADR-0002 (Screen-first)
---
```

## 1. Hardware

### 1.1 — Dispositivi (100 telefoni fisici)

| Item | Quantità | Costo unitario | Costo totale | Note |
|---|---|---|---|---|
| Smartphone Android (refurbished) | 100 | $50-100 | $5.000-10.000 | Samsung A series, Xiaomi Redmi, Moto G — modelli diversificati |
| Dock ricarica multipli | 10 | $100-200 | $1.000-2.000 | Anker 10-port USB charging stations, 60W+ |
| SIM dati prepagate | 100 | $10-20/mese | $1.000-2.000/mese | Italian carrier prepagate (TIM, Vodafone, WindTre, Iliad) |
| Access Point WiFi dedicati | 5 | $100-200 | $500-1.000 | 1 AP per 20 dispositivi |

**Acquisto progressivo**:
- Fase 0-1: 12 telefoni attuali + 3-5 di test
- Fase 2: +10 telefoni (scale a 20-25 identità)
- Fase 3: +20 telefoni (scale a 40-50 identità)
- Fase 4: +50 telefoni (scale a 100 identità)

### 1.2 — Host ADB (5 mini-PC)

| Item | Quantità | Costo unitario | Costo totale | Note |
|---|---|---|---|---|
| Intel NUC 11/12 (o equivalente) | 5 | $200-400 | $1.000-2.000 | 8-16GB RAM, 128GB SSD, WiFi |
| Switch Gigabit | 1 | $50-100 | $50-100 | Connessione host + orchestratore |

### 1.3 — Orchestratore

| Item | Quantità | Costo | Note |
|---|---|---|---|
| Server fisico o cloud VPS | 1 | $500-1.000 (one-time) o $50-100/mese (cloud) | 16-32 core, 64GB RAM, 500GB NVMe |
| GPU (RTX 3060 12GB) | 1 | $300-500 | Per Whisper + Llama 3 8B |

### 1.4 — Infrastruttura fisica

| Item | Quantità | Costo | Note |
|---|---|---|---|
| Rack/shelf per 100 telefoni | 1 | $200-500 | Custom o commerciale |
| Climatizzazione | 1 | $300-800 | 100 dispositivi generano ~1000W di calore |
| Alimentazione (UPS) | 1 | $200-400 | Backup power per orchestratore e host |
| Cablaggio (cavi alimentazione) | 100 | $1-2 | $100-200 totale |

---

## 2. Costi one-time vs ricorrenti

### 2.1 — Costi one-time

| Item | Costo |
|---|---|
| 100 smartphone refurbished | $5.000-10.000 |
| 5 mini-PC host | $1.000-2.000 |
| Dock ricarica + AP WiFi | $1.500-3.000 |
| Server orchestratore | $500-1.000 |
| GPU | $300-500 |
| Rack + climatizzazione + UPS | $700-1.700 |
| **Totale one-time** | **$9.000-18.200** |

### 2.2 — Costi mensili ricorrenti

| Item | Costo/mese |
|---|---|
| SIM dati (100 prepagate) | $1.000-2.000 |
| Server cloud (se non on-prem) | $50-100 |
| GoCardless (PSD2 AIS) | $50-200 |
| 2Captcha (CAPTCHA solving) | $10-50 |
| **Totale mensile** | **$1.110-2.350** |

### 2.3 — Costi evitati (vs approccio API-based)

| Item | Costo evitato | Motivo |
|---|---|---|
| Betfair Live Keys | £49.900 (~€58.000) | Screen-first, zero API |
| PayPal Business upgrade | ~€5.000+ (100 conti) | Screen-first, zero API |
| NinjaBet MultiOdds abbonamento | €19.98/mese × N | Motore interno |
| **Totale risparmio** | **~€60.000+** | |

---

## 3. Capacità e dimensionamento

### 3.1 — Capacità operativa stimata

| Metrica | 10 identità | 100 identità | Note |
|---|---|---|---|
| Riconciliazione (P6) | ~2-4h/mese | ~20-40h/mese (automatizzato) | vs 24-48h manuale per 10 ID |
| Operazioni sport/mese | ~50 | ~500 | Dipende da segnali |
| Operazioni casinò/mese | ~100 | ~1.000 | Dipende da segnali |
| Movimenti/mese | ~200 | ~2.000 | Deposit/prelievi |
| Screenshot/giorno | ~500 | ~5.000 | ScreenReader reads |
| HITL alerts/giorno | ~5-10 | ~50-100 | Dipende da maturità sistema |

### 3.2 — Dimensionamento GPU

| Modello | VRAM | Use case | Note |
|---|---|---|---|
| Whisper large-v3 | ~10GB | Trascrizione think-aloud | Solo quando nuove sessioni |
| Llama 3 8B (quantizzato) | ~6-8GB | Signal Parser + RAG | Continuo |
| Llama 3 8B (full) | ~16GB | Se serve più accuratezza | RTX 4060 Ti 16GB |

**Con RTX 3060 12GB**: può far girare Llama 3 8B quantizzato in continuo, Whisper large-v3 on-demand (non simultanei).
**Con RTX 4060 Ti 16GB**: può far girare entrambi simultaneamente.

---

## 4. Consumo energetico

| Componente | Watt | Quantità | Totale W |
|---|---|---|---|
| Smartphone in carica | 5-15W | 100 | 500-1.500W |
| Mini-PC host | 15-25W | 5 | 75-125W |
| Server orchestratore | 100-200W | 1 | 100-200W |
| GPU | 150-170W | 1 | 150-170W |
| Switch + AP | 10-15W | 6 | 60-90W |
| **Totale** | | | **885-2.085W** |

**Costo elettrico stimato** (0.25 €/kWh): €158-375/mese
