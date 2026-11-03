# Proiect B — Integrarea angajaților noi (Faza 1)

Automatizare completă în Python pentru procesarea cererilor de integrare a angajaților noi (**Grupa 2 — Faza 1**), construită conform specificațiilor din *01 Ghid student*, *Proiect B - Integrarea angajaților noi* și contractului OpenAPI `B-onboarding.json`.

## Cum se rulează la notare

```
image: python:3.12-slim
setup: pip install -r requirements.txt
command: python solutie.py {base_url}
```

## Dependențe

- **Python**: 3.10+ (compatibil și cu Python 3.9+, testat pe Python 3.12).
- **`tzdata>=2024.1`** (`requirements.txt`): baza oficială de fusuri orare IANA pentru modulul standard `zoneinfo`, necesară în containerele Linux minimale (`python:3.12-slim`) și pe Windows pentru calculul offsetului local `Europe/Bucharest` (`+02:00` iarna / `+03:00` vara).
- Toate celelalte funcționalități (client HTTP, serializare JSON, validări, aritmetică calendaristică, teste unitare) folosesc exclusiv biblioteca standard Python.

## Ipoteze și decizii de proiectare

1. **Fără stare locală și fără ceasul sistemului**: soluția nu salvează nimic pe disc între rulări; data de lucru este citită exclusiv din `GET /context` (`business_date`), iar parametrii regulilor (`minimum_age`, `adult_age`, fereastra și intervalele întâlnirii, destinatarii notificărilor) sunt citiți dinamic din `GET /config`.
2. **Contract JSON strict**: în toate apelurile `POST` sunt trimise exclusiv câmpurile documentate în schema OpenAPI, cu valorile normalizate (`N1`–`N6`); câmpurile necunoscute din cereri sau din răspunsurile sistemelor (inclusiv note libere de tip `notes`) sunt ignorate.
3. **Determinism complet la repornire**: listele de pachete, instruiri și coduri de motiv (`reason_codes`) sunt deduplicate și sortate într-o ordine canonică fixă, iar mesajele de notificare sunt construite determinist doar din datele cererii și nu conțin nicio cifră (garantând absența CNP-ului în orice formă).
4. **Ordinea procesării**: cererile din `GET /requests` sunt procesate secvențial în ordinea canonică (`received_at` comparat ca moment în timp, urmat de `request_id`), deoarece conturile și întâlnirile create anterior influențează regulile `B-R08`, `B-R10` și `B-R12`.
5. **Cererea de modificare CR-1 (Transferuri interne)**: activată dinamic doar când `features.transfers` este `true` în `GET /config` (astfel încât suitele fără CR-1 păstrează comportamentul de bază Faza 1 fără regresii). Pentru cererile `TRANSFER`, contul existent este refolosit (`NO_EXISTING_ACCOUNT` dacă lipsește), drepturile active din afara setului țintă sunt revocate prin `POST /access/revocations` înaintea acordării pachetelor noi, iar instruirile finalizate (`COMPLETED`) la sau după `start_date - transfer.training_recency_months` nu se mai reînscriu.

## Structura proiectului

- `solutie.py` — punctul de intrare CLI (primește `BASE_URL` ca prim argument sau din variabila de mediu `BPA_BASE_URL`).
- `onboarding/models.py` — modele de date imutabile, constante și ordonarea canonică a codurilor de motiv.
- `onboarding/http_client.py` — client HTTP cu timeout de 10s, antet `Idempotency-Key` și politica completă de reîncercare (`5xx`, timeout/rețea, `429 RATE_LIMITED`, `429 RUN_REQUEST_LIMIT`, `422 IDEMPOTENCY_KEY_REUSED`).
- `onboarding/normalizer.py` — regulile de normalizare `N1`–`N6` și construcția numelui de utilizator de bază (`B-R10`).
- `onboarding/validator.py` — motorul de validare `B-R01`–`B-R09`, precedența rezultatelor și regulile de omitere.
- `onboarding/calendar_utils.py` — aritmetica zilelor lucrătoare (luni–vineri fără `GET /holidays`), termene LMS (`B-R11`) și căutarea sloturilor libere în fusul `Europe/Bucharest` (`B-R12`).
- `onboarding/provisioner.py` — execuția idempotentă a celor patru pași (`account`, `access`, `training`, `meeting`) și tratarea eșecurilor definitive (`B-R13`).
- `onboarding/notifier.py` — calculul și trimiterea notificărilor lipsă fără CNP.
- `onboarding/engine.py` — orchestratorul fluxului de lucru (pașii 1–10), regula de istoric/repornire `B-R14` și trimiterea raportului final (`POST /report`).
- `tests/test_onboarding.py` — suita de teste unitare și de integrare pe hub simulat în memorie.
- `ARHITECTURA.md` — nota de arhitectură a soluției.
- `public_run.log` — logul soluției obținut la o rulare pe suita publică.

## Cum se rulează local

Instalarea dependenței:

```bash
pip install -r requirements.txt
```

Rularea directă pe un `BASE_URL`:

```bash
python3 solutie.py https://apb.majordom.tech/r/<token>
```

Rularea prin `bpa_runner.py` (cu `BPA_HUB` și `BPA_KEY` setate în mediul shell, niciodată în Git):

```bash
export BPA_HUB=https://apb.majordom.tech
export BPA_KEY=<cheia-personala>
python3 bpa_runner.py --project B --suite public -- python3 solutie.py {base_url}
```

Rularea testelor unitare locale:

```bash
python3 -m unittest discover -s tests -v
```
