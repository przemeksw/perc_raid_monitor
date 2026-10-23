# raid_monitor.py – monitoring RAID, dysków i baterii (Dell PERC H730P / LSI MegaRAID)

Skrypt w Pythonie (tylko biblioteka standardowa) dla Proxmoksa/Debiana. Sprawdza:

- stan kontrolera, macierzy (VD) i wszystkich dysków fizycznych,
- baterię BBU / CacheVault,
- liczniki błędów dysków i ich **trend** (porównanie z historią zapisaną w `/var/lib/raid_monitor`),
- zużycie SSD i szacowaną liczbę dni do wyczerpania zapasu zapisów,
- wysyła mail (wielu odbiorców) z przyczyną, numerem slotu i numerem seryjnym dysku oraz opisem "co to znaczy / co zrobić".

Raport pokazuje dyski pogrupowane wg macierzy, z typem RAID przy każdej grupie.

---

## 1. Wymagania

| Element | Po co | Obowiązkowe |
|---|---|---|
| root | dostęp do kontrolera i smartctl | tak |
| Python 3.7+ | sam skrypt (jest w Proxmoksie, żadne pakiety pip nie są potrzebne) | tak |
| perccli64 (lub storcli64) | odczyt stanu kontrolera RAID | tak |
| smartmontools (`smartctl`) | zużycie SSD, SMART, grown defect list | zalecane |
| dostęp do serwera SMTP (587/465/25) | wysyłka maili | do alertów |
| cron | uruchamianie co 15 minut | do pracy ciągłej |

Sprawdzenie:

```
python3 --version
ls -l /opt/MegaRAID/perccli/perccli64
which smartctl
```

## 2. Instalacja zależności

### 2.1 smartmontools

```
apt update
apt install -y smartmontools
smartctl --version
```

Test odczytu dysku za kontrolerem (DID dysku bierzesz z kolumny DID w raporcie):

```
smartctl -a -d megaraid,10 /dev/sda
```

Jeśli `/dev/sda` nie istnieje, wskaż inne urządzenie leżące na kontrolerze (ustawienie `SMARTCTL_DEVICE`).

### 2.2 perccli (jeśli jeszcze nie jest zainstalowany)

perccli nie jest w repozytoriach Debiana. Pobierz paczkę **PERC CLI** (Linux, RPM) ze strony wsparcia Dell dla swojego kontrolera (lub storcli z Broadcom), a potem:

```
apt install -y alien unzip
unzip perccli_*.zip                       # rozpakuj archiwum z Dell
alien -k --scripts perccli-*.noarch.rpm   # konwersja rpm -> deb
dpkg -i perccli_*.deb
```

Program ląduje w `/opt/MegaRAID/perccli/perccli64`. Test:

```
/opt/MegaRAID/perccli/perccli64 show
/opt/MegaRAID/perccli/perccli64 /c0 show all
```

## 3. Instalacja skryptu

```
cp raid_monitor.py /opt/raid_monitor.py      # albo wklejony przez cat > ... <<'EOF'
chmod 700 /opt/raid_monitor.py
python3 -m py_compile /opt/raid_monitor.py && echo OK
```

## 4. Konfiguracja

Ustawienia można zmieniać na górze skryptu, ale **lepiej trzymać je w osobnym pliku**, który przetrwa aktualizację skryptu:

```
nano /etc/raid_monitor.conf
chmod 600 /etc/raid_monitor.conf
```

Przykład (składnia Pythona, tylko nazwy wielkimi literami, nazwy takie jak w skrypcie):

```
SMTP_HOST = "smtp.twojadomena.pl"
SMTP_PORT = 587
SMTP_SECURITY = "starttls"        # "starttls" | "ssl" | "none"
SMTP_USER = "alerty@twojadomena.pl"
SMTP_PASSWORD = "haslo"
MAIL_FROM = "raid-monitor@twojadomena.pl"
MAIL_TO = ["admin1@twojadomena.pl", "admin2@twojadomena.pl"]
MAIL_SUBJECT_PREFIX = "[ISP] "

PERCCLI = "/opt/MegaRAID/perccli/perccli64"
SMARTCTL = "/usr/sbin/smartctl"
SMARTCTL_DEVICE = "/dev/sda"
```

Plik jest ignorowany, jeśli mogą go zapisywać inni (grupa/pozostali) – stąd `chmod 600`.

Inne przydatne ustawienia (wszystkie można nadpisać w tym pliku):

| Ustawienie | Domyślnie | Znaczenie |
|---|---|---|
| `SSD_DAYS_WARN` / `SSD_DAYS_CRIT` | 365 / 90 | alarm, gdy do wyczerpania SSD zostało mniej dni |
| `SSD_LIFE_WARN` / `SSD_LIFE_CRIT` | 20 / 10 | alarm wg % pozostałego zapasu zapisów |
| `STABLE_DAYS` | 14 | licznik niezmienny tyle dni = tylko informacja |
| `TEMP_WARN` / `TEMP_CRIT` | SSD 60/70, HDD 50/60 | progi temperatury dysków |
| `BBU_TEMP_WARN` / `BBU_TEMP_CRIT` | 50 / 60 | progi temperatury baterii |
| `REMINDER_HOURS` | 24 | przypomnienie przy problemie krytycznym |
| `WARNING_REMINDER_HOURS` | 168 | przypomnienie przy samych ostrzeżeniach |
| `MIN_SEVERITY_MAIL` | `"WARNING"` | `"CRITICAL"` = maile tylko przy krytycznych |
| `STATE_DIR` | `/var/lib/raid_monitor` | stan i historia |

## 5. Pierwsze uruchomienie i test

```
/opt/raid_monitor.py            # raport na konsoli, nic nie zapisuje
/opt/raid_monitor.py test       # wysyła mail kontrolny z pełnym raportem
/opt/raid_monitor.py monitor    # jak z crona: zapis historii + mail tylko przy problemie
```

Po pierwszym `monitor` w `/var/lib/raid_monitor` pojawią się `state.json` i `history.jsonl`.

## 6. Cron

```
echo '*/15 * * * * root /usr/bin/python3 /opt/raid_monitor.py monitor >/dev/null 2>&1' > /etc/cron.d/raid_monitor
chmod 644 /etc/cron.d/raid_monitor
```

Skrypt sprawdza stan co 15 minut, a próbkę do historii zapisuje najwyżej raz na godzinę (`HISTORY_INTERVAL_MIN`).

## 7. Tryby pracy

| Polecenie | Działanie |
|---|---|
| (bez argumentu) | raport na konsoli, bez maila i bez zapisu |
| `monitor` | sprawdzenie, zapis historii, mail przy zmianie problemów |
| `test` | zawsze wysyła mail kontrolny |
| `trend` | zmiana liczników dysków po 1 / 7 / 30 dniach |
| `dump` | surowe odpowiedzi perccli o baterii (diagnostyka) |

Kody wyjścia: `0` OK, `1` WARNING, `2` CRITICAL, `3` błąd wysyłki maila.

## 8. Kiedy przychodzi mail

- **Alarm natychmiast** (wartości bezwzględne): dysk Failed/Offline, alarm SMART dysku, Predictive Failure, macierz Degraded/Offline, zła bateria, zbyt wysoka temperatura.
- **Liczniki błędów** (błędy nośnika, grown defect list, przealokowane sektory): liczy się trend. Niezerowy, ale stały od 14 dni = tylko informacja w raporcie. Rosnący = ostrzeżenie, szybko rosnący = alarm krytyczny.
- **SSD**: alarm, gdy szacowany czas do wyczerpania zapasu zapisów spadnie poniżej progu. To szacunek zużycia komórek flash, nie prognoza losowej awarii elektroniki.
- Mail wychodzi tylko gdy **zmieni się zestaw problemów**, potem jako przypomnienie (24 h krytyczne, 168 h ostrzeżenia). Gdy problemy ustąpią, wychodzi mail "problemy ustąpiły".

## 9. Aktualizacja skryptu

Podmień `/opt/raid_monitor.py`. Konfiguracja w `/etc/raid_monitor.conf` oraz historia w `/var/lib/raid_monitor` zostają. Po podmianie sprawdź:

```
python3 -m py_compile /opt/raid_monitor.py && /opt/raid_monitor.py
```

## 10. Rozwiązywanie problemów

| Objawy | Co sprawdzić |
|---|---|
| `Nie znaleziono programu: ...perccli64` | ścieżka `PERCCLI`, uprawnienia, czy skrypt działa jako root |
| brak kolumny "SSD zostało" | `which smartctl`, ręczny test `smartctl -a -d megaraid,<DID> /dev/sda`, ustawienie `SMARTCTL_DEVICE` |
| brak danych baterii | `/opt/raid_monitor.py dump` i sprawdź `perccli64 /c0/bbu show all` |
| mail nie dochodzi | `/opt/raid_monitor.py test`; sprawdź host, port, tryb (`starttls`/`ssl`), login; `SMTP_VERIFY_TLS = False` przy własnym certyfikacie |
| katalog `/var/lib/raid_monitor` pusty | normalne po uruchomieniu bez argumentu; dane powstają w trybie `monitor` |
| "Pomijam /etc/raid_monitor.conf" | `chmod 600 /etc/raid_monitor.conf` |
| brak ocen trendu | historia musi mieć kilkanaście dni; do tego czasu liczniki mają status "obserwacja" |

## 11. Pliki

| Ścieżka | Zawartość |
|---|---|
| `/opt/raid_monitor.py` | skrypt |
| `/etc/raid_monitor.conf` | opcjonalna konfiguracja (SMTP, odbiorcy, progi) |
| `/etc/cron.d/raid_monitor` | harmonogram |
| `/var/lib/raid_monitor/state.json` | stan powiadomień (ostatni mail, lista problemów) |
| `/var/lib/raid_monitor/history.jsonl` | historia liczników (domyślnie 400 dni) |

## 12. Odinstalowanie

```
rm -f /etc/cron.d/raid_monitor /opt/raid_monitor.py /etc/raid_monitor.conf
rm -rf /var/lib/raid_monitor
```
