# Notă de Arhitectură — Proiect B (Faza 1: Integrarea angajaților noi)

## 1. Componente și separarea responsabilităților

Soluția este organizată modular în jurul unui orchestrator fără stare locală (`stateless`), în care hub-ul este singura sursă de adevăr:

- **`solutie.py` (Interfața CLI)**: preia `BASE_URL` din primul argument sau din `BPA_BASE_URL`, configurează fluxurile UTF-8 pentru logare și gestionează codul de ieșire (`0` la succes, `1` la eroare fatală conform politicii).
- **`onboarding/http_client.py` (`HubClient`)**: izolează întreaga comunicare HTTP cu hub-ul (timeout client de `10s`, serializare JSON deterministă fără câmpuri suplimentare, atașarea antetului `Idempotency-Key`, politica de reîncercare și clasificarea erorilor în recuperabile, eșecuri de pas `B-R13` sau erori fatale).
- **`onboarding/models.py`**: definește modelele tipizate (`RunConfig`, `ReferenceData`, `NormalizedRequest`, `ValidationResult`, `ProvisioningResult`) și tuplul `CANONICAL_REASON_ORDER` care garantează aceeași ordine a codurilor de motiv la fiecare execuție.
- **`onboarding/normalizer.py`**: aplică regulile `N1`–`N6` asupra documentului brut al cererii și construiește forma de bază ASCII a numelui de utilizator.
- **`onboarding/validator.py`**: evaluează regulile `B-R01`–`B-R09`, aplicând graful de precedență (`REJECTED > MANUAL_REVIEW`) și omiterile dintre reguli.
- **`onboarding/calendar_utils.py`**: implementează calculul zilelor lucrătoare (excluzând weekendurile și datele din `GET /holidays`), calculul `due_date` pentru instruiri și generarea/verificarea intervalelor semideschise în fusul `Europe/Bucharest` (`zoneinfo.ZoneInfo`).
- **`onboarding/provisioner.py`**: execută secvențial pașii `account`, `access`, `training` și `meeting`, refolosind resursele deja create pentru `request_id` și oprind controlat pasul curent la eșec definitiv (`3 × HTTP 500`).
- **`onboarding/notifier.py`**: determină perechile `(recipient_role, template)` aferente rezultatului, filtrează notificările deja trimise în rularea curentă și garantează absența CNP-ului.
- **`onboarding/engine.py`**: coordonează pașii 1–10 din fluxul de lucru: încărcarea unică a configurației și cataloagelor, parcurgerea canonică a `GET /requests`, aplicarea regulii `B-R14` pe `GET /history`, salvarea `POST /onboarding-records` înainte de notificări și trimiterea raportului final `POST /report`.

---

## 2. Unde este implementată fiecare regulă

| Regulă | Descriere scurtă | Fișier și funcție |
| :--- | :--- | :--- |
| **N1–N6** | Trim șiruri, majuscule coduri, minuscule e-mail, diacritice cu virgulă și Title Case nume, parsare dată `YYYY-MM-DD` / `DD.MM.YYYY` | `onboarding/normalizer.py` → `normalize_request()`, `normalize_person_name()`, `parse_start_date()` |
| **B-R01** | Formă cerere, câmpuri obligatorii nevide, `request_type == NEW_HIRE`, dată reală, format e-mail (`INVALID_REQUEST`, oprește restul validărilor) | `onboarding/normalizer.py` → `normalize_request()` și `onboarding/validator.py` → `validate_request()` |
| **B-R02** | Validare CNP: 13 cifre, `S ∈ 1..6`, dată naștere reală pe secol, județ `01..46`/`51`/`52`, `NNN != 000`, cifră control mod 11 (`INVALID_CNP`; omite `B-R03`, `B-R08`) | `onboarding/validator.py` → `parse_and_validate_cnp()`, `validate_request()` |
| **B-R03** | Vârsta împlinită la `start_date` față de `minimum_age` / `adult_age` din `GET /config`, inclusiv 29 feb → 1 mar în ani nebisecți (`UNDERAGE`, `MINOR_EMPLOYEE`) | `onboarding/validator.py` → `compute_completed_years()`, `validate_request()` |
| **B-R04** | Existența rolului în `GET /roles` și a departamentului în `GET /departments` (`UNKNOWN_ROLE`, `UNKNOWN_DEPARTMENT`; omite `B-R06`, `B-R09`) | `onboarding/validator.py` → `validate_request()` |
| **B-R05** | `start_date < business_date` din `GET /context` (`START_DATE_IN_PAST`) | `onboarding/validator.py` → `validate_request()` |
| **B-R06** | `department_code` inclus în `allowed_departments` al rolului (`ROLE_NOT_IN_DEPARTMENT`; omite `B-R09`) | `onboarding/validator.py` → `validate_request()` |
| **B-R07** | Existența `manager_id` în `GET /employees` și starea `ACTIVE` (`MANAGER_MISSING`, `MANAGER_INACTIVE`) | `onboarding/validator.py` → `validate_request()` |
| **B-R08** | Verificare live `GET /directory/accounts?cnp=...` pentru cont existent cu alt `request_id` (`ALREADY_EMPLOYED`) | `onboarding/validator.py` → `validate_request()` |
| **B-R09** | Reuniune sortată `base_packages ∪ addon_packages`, detecție conflict `conflicts_with` (`SOD_CONFLICT`) și acordarea pachetelor lipsă | `onboarding/validator.py` → `validate_request()` și `onboarding/provisioner.py` → `provision_onboarding()` |
| **B-R10** | Reutilizare cont `(cnp, request_id)` sau creare cont cu slug ASCII `prenume.nume-familie` și sufix `2, 3, ...` la coliziune în Directory | `onboarding/normalizer.py` → `generate_base_username()` și `onboarding/provisioner.py` → `_pick_free_username()`, `provision_onboarding()` |
| **B-R11** | Reuniune sortată instruiri `mandatory` + `required_trainings` și calcul `due_date` în zile lucrătoare după `start_date` | `onboarding/calendar_utils.py` → `add_working_days()` și `onboarding/provisioner.py` → `provision_onboarding()` |
| **B-R12** | Fereastra de `window_working_days` de la `start_date` inclusiv, sloturi `Europe/Bucharest`, intervale semideschise peste `GET /calendar/busy` (`DONE` / `NO_SLOT` + `MEETING_NOT_SCHEDULED`) | `onboarding/calendar_utils.py` → `get_meeting_window_days()`, `find_first_free_meeting_slot()` și `onboarding/provisioner.py` |
| **B-R13** | Tratarea eșecurilor definitive (`3 × 500`) pe cele 4 scrieri de configurare: `ACCOUNT_FAILED` sare pașii rămași (`SKIPPED`); celelalte opresc doar pasul propriu și duc la `PARTIAL` | `onboarding/http_client.py` (`ProvisioningStepError`) și `onboarding/provisioner.py` → `provision_onboarding()` |
| **B-R14** | Decizia pe ultima înregistrare din `GET /history`: `THIS_RUN` trimite doar notificările lipsă; `EARLIER_RUN` finalizat nu scrie nimic; `EARLIER_RUN` cu `PARTIAL` refolosește pașii existenți | `onboarding/engine.py` → `run_onboarding()` și `onboarding/notifier.py` → `send_missing_notifications()` |

---

## 3. Reîncercări, chei de idempotență și reporniri

### Politica de reîncercare (`HubClient._request`)
Fiecare cerere HTTP folosește un timeout de client de **10 secunde** și aplică local mașina de stări:
- **`500, 502, 503, 504`, timeout sau eroare de rețea**: cel mult **3 încercări în total** (`attempt = 1..3`). Înainte de retrimitere se așteaptă `Retry-After` secunde (`1.0s` dacă antetul lipsește) și se scrie o linie `RETRY ...` în log. La `POST`, se retrimit **exact aceeași cheie `Idempotency-Key` și același corp JSON**. `503 RUN_PAUSED` este tratat ca orice `503`, permițând runner-ului să oprească procesul în punctul de întrerupere.
- **`429 RATE_LIMITED`**: se așteaptă `Retry-After` (`1.0s` implicit), se scrie în log și se retrimite apelul **fără a consuma** una dintre cele 3 încercări, până la maximum **10 așteptări** pe apel.
- **`429 RUN_REQUEST_LIMIT` și alte erori fatale**: opresc imediat execuția cu cod de ieșire `1`, fără a trimite scrieri suplimentare și fără a inventa rezultate de business. Doar cele 4 apeluri `POST` de configurare (`allow_provisioning_failure=True`) convertesc epuizarea celor 3 încercări cu `HTTP 500` în `ProvisioningStepError` (`B-R13`).

### Cheile de idempotență
Cheile nu conțin niciodată valori aleatoare sau marcaje de timp, fiind derivate strict din identificatorii entităților:
- Cont Directory: `{request_id}:account`
- Acordare pachet: `{request_id}:grant:{package_code}`
- Înscriere instruire: `{request_id}:training:{training_code}`
- Întâlnire calendar: `{request_id}:meeting`
- Înregistrare cerere: `{request_id}:record`
- Notificare: `{request_id}:notify:{recipient_role}:{template}`
- Raport final: `report` (iar dacă o pornire anterioară a trimis un raport parțial/diferit și hub-ul răspunde `422 IDEMPOTENCY_KEY_REUSED`, `_submit_final_report()` avansează determinist la `report-2`, `report-3`, ...).

### Mecanismul de repornire (Crash Recovery fără stare locală)
Dacă procesul este oprit în orice punct și repornit pe același `BASE_URL`:
1. La fiecare cerere, `engine.py` citește `GET /history?request_id=...` și inspectează **ultima** înregistrare (`B-R14`).
2. Dacă întreruperea s-a produs **după** `POST /onboarding-records` (`source == THIS_RUN`), validarea și configurarea nu se mai repetă; `notifier.py` citește `GET /notifications?request_id=...` și trimite exclusiv perechile `(recipient_role, template)` care lipsesc, evitând duplicatele chiar și atunci când o pornire anterioară nu are răspunsuri memorate în cache-ul de replay al hub-ului.
3. Dacă întreruperea s-a produs **în timpul** configurării (înainte de `POST /onboarding-records`), `provisioner.py` interoghează starea fiecărui sistem (`GET /directory/accounts?cnp=...`, `GET /access/grants?request_id=...`, `GET /lms/enrollments?request_id=...`, `GET /calendar/meetings?request_id=...`), refolosește integral resursele deja create pentru `request_id` (fără a alege alt `username` sau alt interval orar) și execută `POST` doar pentru elementele rămase neînregistrate.
4. La final, `POST /report` include toate cererile din `GET /requests` (atât cele procesate acum, cât și cele recuperate prin `THIS_RUN` sau finalizate în `EARLIER_RUN`), ducând rularea exact în aceeași stare finală ca o execuție neîntreruptă.
