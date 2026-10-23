#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
raid_monitor.py - monitoring macierzy RAID i dysków (Dell PERC / LSI MegaRAID) przez perccli/storcli.

Użycie:
    ./raid_monitor.py              - raport na konsoli (bez maila, bez zapisu historii)
    ./raid_monitor.py monitor      - sprawdzenie + zapis historii + mail tylko przy problemie (do crona)
    ./raid_monitor.py test         - zawsze wysyła mail z pełnym raportem (test SMTP)
    ./raid_monitor.py trend        - porównanie liczników dysków z historią (po 1 / 7 / 30 dniach)
    ./raid_monitor.py dump         - surowe odpowiedzi perccli o baterii (diagnostyka)

Kody wyjścia: 0 = OK, 1 = WARNING, 2 = CRITICAL, 3 = błąd wysyłki maila.

Jak skrypt decyduje o alarmie:
  * wartości bezwzględne (dysk Failed, alarm SMART, predykcja awarii, temperatura, bateria...) -> alarm od razu,
  * liczniki błędów (błędy nośnika, uszkodzone sektory...) porównuje z historią zapisaną w STATE_DIR:
    liczba niezerowa, ale STABILNA od kilkunastu dni = tylko informacja; liczba, która ROŚNIE = alarm,
  * SSD: z pozostałego zapasu zapisów i tempa zużycia liczy szacowaną liczbę dni do wyczerpania.
  Historia powstaje tylko w trybie monitor (cron) - im dłużej działa, tym dokładniejsza ocena trendu.

Przykład crona (co 15 minut):
    */15 * * * * /usr/bin/python3 /opt/raid_monitor.py monitor >/dev/null 2>&1

WYMAGANIA NA PROXMOXIE (Debian), uruchamiać jako root:

  1. Python 3.7+ - jest domyślnie w Proxmoksie (python3 --version). Tylko biblioteka standardowa,
     żadne pakiety z pip nie są potrzebne.
  2. perccli (Dell PERC) lub storcli (LSI/Broadcom) - narzędzie do kontrolera RAID.
     Nie ma go w repozytoriach Debiana: pobierz z Dell/Broadcom, zainstaluj z paczki
     (alien -k --scripts plik.rpm && dpkg -i plik.deb), ścieżkę wpisz w PERCCLI niżej.
  3. smartmontools (smartctl) - odczyt zużycia SSD i atrybutów SMART dysków za kontrolerem
     (-d megaraid,N):   apt install -y smartmontools
     Bez niego skrypt działa, ale bez predykcji zużycia SSD (albo USE_SMARTCTL = False).
  4. Dostęp do serwera SMTP (587/465/25) z Proxmoksa - sprawdź: ./raid_monitor.py test

  Skrypt musi działać jako root. Dane (stan i historia) trafiają do STATE_DIR (/var/lib/raid_monitor).
  W pliku jest hasło SMTP: chmod 700. Ustawienia możesz też trzymać poza skryptem, w pliku
  /etc/raid_monitor.conf (ten sam zapis NAZWA = wartość, chmod 600) - wtedy nowa wersja skryptu
  nie wymaga ponownego wpisywania konfiguracji.
"""

import argparse
import json
import os
import re
import smtplib
import socket
import ssl
import subprocess
import sys
import textwrap
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

# =============================================================================
# KONFIGURACJA
# =============================================================================

# --- SMTP ---
SMTP_HOST = "smtp.example.com"
SMTP_PORT = 587
SMTP_SECURITY = "starttls"          # "starttls" | "ssl" | "none"
SMTP_VERIFY_TLS = True              # False = nie sprawdzaj certyfikatu serwera SMTP
SMTP_USER = "alerty@example.com"    # "" = bez logowania
SMTP_PASSWORD = "HASLO"
SMTP_TIMEOUT = 30

MAIL_FROM = "raid-monitor@example.com"
MAIL_TO = [                         # dowolna liczba odbiorców
    "admin1@example.com",
    "admin2@example.com",
]
MAIL_SUBJECT_PREFIX = ""            # np. "[ISP] "

# --- narzędzia ---
PERCCLI = "/opt/MegaRAID/perccli/perccli64"   # może być też storcli64
USE_SMARTCTL = True                 # dodatkowe dane SMART/zużycie SSD przez smartctl
SMARTCTL = "/usr/sbin/smartctl"
# Dowolne urządzenie blokowe (wirtualny dysk) leżące na kontrolerze; smartctl przez
# "-d megaraid,<DID>" odpytuje fizyczne dyski za jego pośrednictwem.
SMARTCTL_DEVICE = "/dev/sda"
CMD_TIMEOUT = 120
CONFIG_FILE = "/etc/raid_monitor.conf"        # opcjonalne nadpisanie ustawień (patrz opis na górze)

# --- dane i historia ---
STATE_DIR = "/var/lib/raid_monitor"  # state.json + history.jsonl
HISTORY_INTERVAL_MIN = 60           # co ile minut zapisywać próbkę historii (poza zmianą stanu)
HISTORY_KEEP_DAYS = 400             # jak długo trzymać historię

# --- powiadomienia ---
MIN_SEVERITY_MAIL = "WARNING"       # "WARNING" (też ostrzeżenia/predykcja) albo "CRITICAL"
REMINDER_HOURS = 24                 # przypomnienie, gdy trwa problem KRYTYCZNY
WARNING_REMINDER_HOURS = 168        # przypomnienie, gdy trwają same ostrzeżenia (tydzień)

# --- analiza trendu liczników ---
TREND_WINDOW_DAYS = 30              # wzrost licznika w tym oknie = ostrzeżenie
TREND_FAST_DAYS = 7                 # okno szybkiego wzrostu
STABLE_DAYS = 14                    # licznik niezmienny tyle dni = "stabilny" (bez alarmu)
MEDIA_ERR_ABS_WARN = 20             # Media Error Count: ostrzeżenie bez względu na trend
MEDIA_ERR_ABS_CRIT = 100
MEDIA_ERR_FAST_GROWTH = 10          # przyrost w TREND_FAST_DAYS => alarm krytyczny
OTHER_ERR_GROWTH_WARN = 5           # Other Error Count: przyrost w oknie => ostrzeżenie
GROWN_ABS_WARN = 50                 # SAS: lista uszkodzonych sektorów - próg bezwzględny
GROWN_ABS_CRIT = 200
GROWN_GROWTH_WARN = 1               # SAS: przyrost w oknie => ostrzeżenie
GROWN_FAST_GROWTH = 5               # SAS: przyrost w TREND_FAST_DAYS => alarm krytyczny
HDD_REALLOC_ABS_WARN = 50           # przealokowane sektory HDD - próg bezwzględny
SMART_FAST_GROWTH = 10              # inne liczniki SMART: przyrost w TREND_FAST_DAYS => krytyczny
SMART_UNCORR_FAST_GROWTH = 3        # sektory nieodczytywalne: przyrost w TREND_FAST_DAYS => krytyczny

# --- progi SSD / temperatury ---
SSD_LIFE_WARN = 20                  # % pozostałego zapasu zapisów SSD
SSD_LIFE_CRIT = 10
SPARE_WARN = 20                     # % pozostałej rezerwy (Available_Reservd_Space)
SSD_DAYS_WARN = 365                 # ostrzeżenie, gdy szacowany czas do wyczerpania SSD < tyle dni
SSD_DAYS_CRIT = 90                  # alarm krytyczny, gdy < tyle dni
ETA_WINDOW_DAYS = 90                # z ilu ostatnich dni liczyć bieżące tempo zużycia
ETA_MIN_HISTORY_DAYS = 7            # minimalna długość historii, by użyć bieżącego tempa
ETA_CAP_DAYS = 3650                 # powyżej pokazuj ">3650 dni"
TEMP_WARN = {"SSD": 60, "HDD": 50}
TEMP_CRIT = {"SSD": 70, "HDD": 60}
BBU_TEMP_WARN = 50                  # temperatura baterii BBU / CacheVault
BBU_TEMP_CRIT = 60

# =============================================================================

SEV_RANK = {"OK": 0, "INFO": 0, "WARNING": 1, "CRITICAL": 2}
PD_OK_STATES = ("Onln", "UGood", "GHS", "DHS", "JBOD")
PD_CRIT_STATES = ("UBad", "Failed", "Offln", "Msng")
HOST = socket.getfqdn() or socket.gethostname()

LIFE_ATTRS = ("Media_Wearout_Indicator", "Wear_Leveling_Count", "SSD_Life_Left",
              "Percent_Lifetime_Remain", "Remaining_Lifetime_Perc",
              "Percent_Life_Remaining", "Perc_Rated_Life_Used")
# WD (SA500/Blue/Red SSD): atrybut 230 Media_Wearout_Indicator = % zużycia (wartość rośnie),
# w odróżnieniu od Intela/Samsunga, gdzie to % pozostałej żywotności.
WD_WEAR_IS_USED = re.compile(r"^WDC\s+WDS", re.I)
SPARE_ATTRS = ("Available_Reservd_Space",)
DEFECT_ATTRS = ("Reallocated_Sector_Ct", "Reallocated_Event_Count", "Reported_Uncorrect",
                "Current_Pending_Sector", "Offline_Uncorrectable", "Runtime_Bad_Block",
                "Program_Fail_Cnt_Total", "Erase_Fail_Count_Total", "Uncorrectable_Error_Cnt",
                "Program_Fail_Count", "Erase_Fail_Count", "Grown_Bad_Blocks")
UNCORRECTABLE_ATTRS = ("Reported_Uncorrect", "Current_Pending_Sector", "Offline_Uncorrectable",
                       "Uncorrectable_Error_Cnt")


class ToolError(Exception):
    pass


def load_overrides():
    """Nadpisanie ustawień z pliku CONFIG_FILE (opcjonalne)."""
    path = os.environ.get("RAID_MONITOR_CONF", CONFIG_FILE)
    if not os.path.isfile(path):
        return
    if os.stat(path).st_mode & 0o022:
        sys.stderr.write("Pomijam %s: plik zapisywalny przez innych (chmod 600)\n" % path)
        return
    ns = {}
    with open(path, encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), ns)
    g = globals()
    for k, v in ns.items():
        if k.isupper():
            if k in g:
                g[k] = v
            else:
                sys.stderr.write("Nieznane ustawienie w %s: %s\n" % (path, k))


# -----------------------------------------------------------------------------
# uruchamianie narzędzi
# -----------------------------------------------------------------------------

def run_cmd(cmd, timeout=None):
    timeout = timeout or CMD_TIMEOUT
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, universal_newlines=True)
    except FileNotFoundError:
        raise ToolError("Nie znaleziono programu: %s" % cmd[0])
    except subprocess.TimeoutExpired:
        raise ToolError("Timeout (%ss) polecenia: %s" % (timeout, " ".join(cmd)))
    return p.returncode, p.stdout


def perc(args):
    """Zwraca (ok, response_data, opis). Rzuca ToolError przy braku/uszkodzonym wyjściu."""
    rc, out = run_cmd([PERCCLI] + args + ["J"])
    i = out.find("{")
    if i < 0:
        raise ToolError("perccli %s: brak JSON w odpowiedzi: %s" % (" ".join(args), out.strip()[:300]))
    try:
        j = json.loads(out[i:])
        ctrl = j["Controllers"][0]
        cs = ctrl["Command Status"]
    except (ValueError, KeyError, IndexError) as e:
        raise ToolError("perccli %s: nieczytelna odpowiedź (%s)" % (" ".join(args), e))
    ok = cs.get("Status") == "Success"
    desc = cs.get("Description") or cs.get("Detailed Status") or ""
    return ok, ctrl.get("Response Data", {}) or {}, str(desc)


def toint(v, default=0):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        m = re.search(r"-?\d+", str(v))
        return int(m.group(0)) if m else default


def first_dict(v):
    if isinstance(v, list):
        return v[0] if v else {}
    return v if isinstance(v, dict) else {}


def props(v):
    """Sekcje baterii bywają listą par {"Property": k, "Value": v} - zamień na słownik."""
    if isinstance(v, list) and v and all(isinstance(x, dict) and "Property" in x for x in v):
        return {str(x["Property"]).strip(): x.get("Value") for x in v}
    return first_dict(v)


# -----------------------------------------------------------------------------
# SMART (smartctl przez megaraid)
# -----------------------------------------------------------------------------

def smart_info(did, model=""):
    info = {"available": False, "health": None, "life": None, "spare": None,
            "defects": {}, "failing": [], "grown_defects": None, "temp": None, "poh": None, "note": ""}
    try:
        rc, out = run_cmd([SMARTCTL, "-a", "-d", "megaraid,%s" % did, SMARTCTL_DEVICE], 60)
    except ToolError as e:
        info["note"] = str(e)
        return info
    if "SMART" not in out and "Percentage used" not in out:
        info["note"] = out.strip().splitlines()[-1][:200] if out.strip() else "pusta odpowiedź smartctl"
        return info
    info["available"] = True

    m = re.search(r"SMART overall-health self-assessment test result:\s*(\S+)", out)
    if not m:
        m = re.search(r"SMART Health Status:\s*(\S+)", out)
    if m:
        info["health"] = m.group(1).upper() in ("PASSED", "OK")

    lifes, spares = [], []
    for line in out.splitlines():
        # THRESH bywa "---" (np. WD Blue/Red SSD), stąd (\d+|-+)
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+0x[0-9a-fA-F]+\s+(\d+)\s+(\d+)\s+(\d+|-+)\s+\S+\s+\S+\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        name, value = m.group(2), int(m.group(3))
        thresh = int(m.group(5)) if m.group(5).isdigit() else 0
        rm = re.match(r"\d+", m.group(7).strip())
        raw = int(rm.group(0)) if rm else 0
        if name == "Media_Wearout_Indicator" and WD_WEAR_IS_USED.match(model):
            lifes.append(max(0, 100 - value))   # WD SA500/Blue/Red: wartość = % ZUŻYCIA, nie pozostałości
        elif name in LIFE_ATTRS:
            lifes.append(value)
        if name == "Power_On_Hours":
            info["poh"] = raw
        if name in ("Temperature_Celsius", "Airflow_Temperature_Cel") and info["temp"] is None:
            tm = re.match(r"\d+", m.group(7).strip())
            if tm:
                info["temp"] = int(tm.group(0))
        if name in SPARE_ATTRS:
            spares.append(value)
        if name in DEFECT_ATTRS:
            info["defects"][name] = raw
        if thresh > 0 and value <= thresh and name not in ("Wear_Leveling_Count",) or \
                (m.group(6) not in ("-", "")):
            info["failing"].append("%s (wartość %d, próg %d, when_failed=%s)"
                                   % (name, value, thresh, m.group(6)))
    m = re.search(r"Percentage used endurance indicator:\s*(\d+)\s*%", out)
    if m:
        lifes.append(max(0, 100 - int(m.group(1))))
    m = re.search(r"Accumulated power on time, hours:minutes\s+(\d+):", out)
    if m and info["poh"] is None:
        info["poh"] = int(m.group(1))
    m = re.search(r"Current Drive Temperature:\s*(\d+)", out)
    if m and info["temp"] is None:
        info["temp"] = int(m.group(1))
    m = re.search(r"Elements in grown defect list:\s*(\d+)", out)
    if m:
        info["grown_defects"] = int(m.group(1))
    if lifes:
        info["life"] = min(lifes)
    if spares:
        info["spare"] = min(spares)
    return info


# -----------------------------------------------------------------------------
# zbieranie danych
# -----------------------------------------------------------------------------

def collect_drives(c):
    ok, rd, desc = perc(["/c%d/eall/sall" % c, "show", "all"])
    if not ok:
        ok, rd, desc = perc(["/c%d/sall" % c, "show", "all"])   # backplane bez enclosure
    drives = []
    if not ok:
        return drives
    for key, val in rd.items():
        m = re.match(r"^Drive (/c\d+(?:/e\d+)?/s\d+)$", key)
        if not m:
            continue
        path = m.group(1)
        base = first_dict(val)
        det = rd.get("Drive %s - Detailed Information" % path, {}) or {}
        st = det.get("Drive %s State" % path, {}) or {}
        at = det.get("Drive %s Device attributes" % path, {}) or {}
        d = {
            "ctrl": c, "path": path,
            "slot": str(base.get("EID:Slt", "")).strip(),
            "did": base.get("DID"),
            "state": str(base.get("State", "?")).strip(),
            "dg": str(base.get("DG", "-")).strip(),
            "size": str(base.get("Size", "?")).strip(),
            "intf": str(base.get("Intf", "?")).strip(),
            "med": str(base.get("Med", "?")).strip(),
            "model": str(base.get("Model", "?")).strip(),
            "sn": str(at.get("SN", "?")).strip(),
            "fw": str(at.get("Firmware Revision", "?")).strip(),
            "media_err": toint(st.get("Media Error Count", 0)),
            "other_err": toint(st.get("Other Error Count", 0)),
            "pred_fail": toint(st.get("Predictive Failure Count", 0)),
            "smart_alert": str(st.get("S.M.A.R.T alert flagged by drive", "No")).strip(),
            "temp": toint(re.sub(r"\(.*", "", str(st.get("Drive Temperature", ""))), None)
                    if st.get("Drive Temperature") else None,
            "smart": None, "issues": [], "trend": [], "eta_days": None, "eta_basis": "",
        }
        d["sid"] = d["sn"] if d["sn"] not in ("?", "") else path
        drives.append(d)
    drives.sort(key=lambda x: [toint(p) for p in re.split(r"[:/]", x["slot"] or "0")])
    return drives


def collect():
    res = {"controllers": [], "issues": [], "time": datetime.now()}
    ok, data, desc = perc(["show", "ctrlcount"])
    count = toint(data.get("Controller Count", 0))
    if count == 0:
        raise ToolError("perccli nie widzi żadnego kontrolera RAID (%s)" % desc)

    for c in range(count):
        ctl = {"id": c, "model": "?", "serial": "?", "fw": "?", "status": "?",
               "vds": [], "drives": [], "battery": []}
        ok, rd, desc = perc(["/c%d" % c, "show", "all"])
        if not ok:
            raise ToolError("Nie można odczytać kontrolera c%d: %s" % (c, desc))
        b = rd.get("Basics", {})
        ctl["model"] = str(b.get("Model", "?"))
        ctl["serial"] = str(b.get("Serial Number", "?"))
        ctl["fw"] = str(rd.get("Version", {}).get("Firmware Version", "?"))
        stt = rd.get("Status", {})
        ctl["status"] = str(stt.get("Controller Status", "?"))
        ctl["mem_unc"] = toint(stt.get("Memory Uncorrectable Errors", 0))

        ok, vd, _ = perc(["/c%d/vall" % c, "show"])
        if ok:
            ctl["vds"] = vd.get("Virtual Drives", []) or []
        ctl["drives"] = collect_drives(c)

        for sub, key in (("bbu", "BBU_Info"), ("cv", "Cachevault_Info")):
            try:
                ok, bd, _ = perc(["/c%d/%s" % (c, sub), "show", "all"])
            except ToolError:
                continue
            if ok and bd.get(key):
                ctl["battery"].append({
                    "kind": "BBU" if sub == "bbu" else "CacheVault",
                    "info": props(bd[key]),
                    "fw": props(bd.get("BBU_Firmware_Status") or bd.get("Firmware_Status")),
                    "cap": props(bd.get("BBU_Capacity_Info") or bd.get("Capacity_Info")),
                    "gas": props(bd.get("GasGaugeStatus")),
                })

        res["controllers"].append(ctl)
    return res


# -----------------------------------------------------------------------------
# magazyn: stan i historia (STATE_DIR)
# -----------------------------------------------------------------------------

def ensure_state_dir():
    global STATE_DIR
    for path in (STATE_DIR, "/var/tmp/raid_monitor"):
        try:
            os.makedirs(path, 0o700, exist_ok=True)
            if os.access(path, os.W_OK):
                if path != STATE_DIR:
                    sys.stderr.write("Uwaga: używam %s zamiast %s\n" % (path, STATE_DIR))
                STATE_DIR = path
                return path
        except OSError:
            pass
    return None


def _path(name):
    return os.path.join(STATE_DIR, name)


def load_state():
    try:
        with open(_path("state.json")) as f:
            return json.load(f)
    except (IOError, ValueError):
        return {}


def save_state(st):
    try:
        tmp = _path("state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, _path("state.json"))
    except (IOError, OSError) as e:
        sys.stderr.write("Nie można zapisać stanu w %s: %s\n" % (STATE_DIR, e))


def load_history(now):
    recs = []
    try:
        with open(_path("history.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if "t" in r and now - r["t"] <= HISTORY_KEEP_DAYS * 86400:
                    recs.append(r)
    except IOError:
        pass
    recs.sort(key=lambda r: r["t"])
    return recs


def build_record(res, now):
    rec = {"t": int(now), "sev": res["severity"], "drives": {}, "vds": {}, "bat": {},
           "keys": sorted("%s|%s" % (i["sev"], i["key"]) for i in res["issues"] if SEV_RANK[i["sev"]] > 0)}
    for ctl in res["controllers"]:
        for vd in ctl["vds"]:
            rec["vds"]["c%d:%s" % (ctl["id"], vd.get("DG/VD"))] = vd.get("State")
        for bat in ctl["battery"]:
            rec["bat"]["c%d:%s" % (ctl["id"], bat["kind"])] = {
                "state": bat["info"].get("State") or bat["info"].get("Battery State"),
                "charge": bat["cap"].get("Relative State of Charge") or bat["gas"].get("Relative State of Charge")}
        for d in ctl["drives"]:
            r = {"slot": d["slot"], "st": d["state"], "media_err": d["media_err"],
                 "other_err": d["other_err"], "pred_fail": d["pred_fail"], "temp": d["temp"]}
            s = d.get("smart")
            if s and s["available"]:
                r["life"], r["poh"], r["grown"] = s["life"], s["poh"], s["grown_defects"]
                for k, v in s["defects"].items():
                    r["smart_" + k] = v
            rec["drives"][d["sid"]] = {k: v for k, v in r.items() if v is not None}
    return rec


def write_history(rec, history, state, now):
    """Dopisuje próbkę (co HISTORY_INTERVAL_MIN lub przy zmianie zestawu problemów); raz na dobę czyści stare."""
    last = history[-1] if history else None
    due = (last is None or now - last["t"] >= HISTORY_INTERVAL_MIN * 60 or last.get("keys") != rec["keys"])
    prune = now - state.get("last_prune", 0) > 86400
    if not due and not prune:
        return
    try:
        if prune:
            tmp = _path("history.jsonl.tmp")
            with open(tmp, "w") as f:
                for r in history + ([rec] if due else []):
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")
            os.replace(tmp, _path("history.jsonl"))
            state["last_prune"] = now
        else:
            with open(_path("history.jsonl"), "a") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        if due:
            history.append(rec)
    except (IOError, OSError) as e:
        sys.stderr.write("Nie można zapisać historii w %s: %s\n" % (STATE_DIR, e))


def delta_over(history, sid, field, cur, now, days):
    """(przyrost, ile dni pokrywa historia) względem najstarszej próbki z ostatnich `days` dni; None = brak historii."""
    for r in history:
        if now - r["t"] > days * 86400:
            continue
        v = r.get("drives", {}).get(sid, {}).get(field)
        if v is None:
            continue
        if r["t"] >= now:
            return None
        return cur - v, (now - r["t"]) / 86400.0
    return None


# -----------------------------------------------------------------------------
# ocena stanu
# -----------------------------------------------------------------------------

def add_issue(res, sev, key, text, drive=None, vd=None, ctl=None, battery=None):
    res["issues"].append({"sev": sev, "key": key, "text": text,
                          "drive": drive, "vd": vd, "ctl": ctl, "battery": battery})
    if drive is not None:
        drive["issues"].append((sev, text))


def check_counter(res, d, sid, ident, field, label, cur, history, now,
                  abs_warn=None, abs_crit=None, growth_warn=1, fast_growth=None, watch=True):
    """Ocena licznika na podstawie wartości bezwzględnej ORAZ trendu z historii.
    kind w kluczu: abs (próg), fast (szybki wzrost), incr (wzrost), stable (niezerowy, ale stały), obs (za krótka historia)."""
    def key(kind):
        return "%s|%s_%s" % (sid, field, kind)

    dw = delta_over(history, sid, field, cur, now, TREND_WINDOW_DAYS)
    df = delta_over(history, sid, field, cur, now, TREND_FAST_DAYS)
    if dw:
        d["trend"].append("%s: %d (w ciągu %.0f dni: %+d)" % (label, cur, dw[1], dw[0]))
    elif cur:
        d["trend"].append("%s: %d (brak historii do porównania)" % (label, cur))

    if abs_crit is not None and cur >= abs_crit:
        add_issue(res, "CRITICAL", key("abs"),
                  "%s: %s = %d (próg krytyczny %d)" % (ident, label, cur, abs_crit), drive=d)
    elif fast_growth and df and df[0] >= fast_growth:
        add_issue(res, "CRITICAL", key("fast"),
                  "%s: %s rośnie szybko: +%d w ciągu %.0f dni (obecnie %d)" % (ident, label, df[0], df[1], cur), drive=d)
    elif growth_warn and dw and dw[0] >= growth_warn:
        add_issue(res, "WARNING", key("incr"),
                  "%s: %s wzrósł o %d w ciągu %.0f dni (obecnie %d)" % (ident, label, dw[0], dw[1], cur), drive=d)
    elif abs_warn is not None and cur >= abs_warn:
        add_issue(res, "WARNING", key("abs"),
                  "%s: %s = %d (przekroczony próg %d)" % (ident, label, cur, abs_warn), drive=d)
    elif watch and cur > 0:
        if dw and dw[1] >= STABLE_DAYS and dw[0] == 0:
            add_issue(res, "INFO", key("stable"),
                      "%s: %s = %d, bez zmian od %.0f dni - stabilny" % (ident, label, cur, dw[1]), drive=d)
        else:
            add_issue(res, "INFO", key("obs"),
                      "%s: %s = %d (obserwacja: historia krótsza niż %d dni)" % (ident, label, cur, STABLE_DAYS), drive=d)


def estimate_days(life, poh, hist, now):
    """Szacunek (dni, podstawa) do wyczerpania zapasu zapisów SSD przy obecnym tempie zużycia.
    Bierze pod uwagę: średnie tempo od początku życia dysku (z godzin pracy) oraz tempo z ostatnich
    tygodni (z historii). Zwraca bardziej pesymistyczny wynik."""
    cands = []
    used = 100 - life
    if poh and used > 0:
        per_day = used / (poh / 24.0)
        cands.append((life / per_day, "średnie tempo od początku życia dysku"))
    win = [h for h in hist if now - h[0] <= ETA_WINDOW_DAYS * 86400]
    if win:
        t0, l0 = min(win, key=lambda h: h[0])
        age = (now - t0) / 86400.0
        if age >= ETA_MIN_HISTORY_DAYS and l0 > life:
            cands.append((life / ((l0 - life) / age), "tempo z ostatnich %d dni" % int(age)))
    return min(cands) if cands else None


def eta_text(d):
    e = d.get("eta_days")
    if e is None:
        return "-"
    return ">%d dni" % ETA_CAP_DAYS if e > ETA_CAP_DAYS else "~%d dni" % int(round(e))


BAT_CRIT_FLAGS = ("Battery Pack Missing", "Pack is about to fail & should be replaced",
                  "Replacement required")
BAT_WARN_FLAGS = ("Remaining Capacity Low", "Learn Cycle Timeout", "I2c Errors Detected",
                  "No space to cache offload")
BAT_OK_STATUS = ("Voltage", "Temperature", "Learn Cycle Status", "NVCache State")


def evaluate_battery(res, ctl, bat):
    c, kind = ctl["id"], bat["kind"]
    info, fw, gas = bat["info"], bat["fw"], bat["gas"]
    fwl = {str(k).lower(): str(v).strip() for k, v in fw.items()}      # nazwy bez rozróżniania wielkości liter
    gasl = {str(k).lower(): str(v).strip() for k, v in gas.items()}
    tail = " (cache zapisu może być wyłączony - spadek wydajności, ryzyko utraty danych z cache przy awarii zasilania)"
    st = str(info.get("State") or info.get("Battery State") or "").strip()
    if st and st not in ("Optimal", "Operational", "Learning", "Charging"):
        sev = "CRITICAL" if st in ("Failed", "Missing", "Not Present") else "WARNING"
        add_issue(res, sev, "c%d|%s|state" % (c, kind),
                  "Kontroler c%d: %s w stanie '%s'%s" % (c, kind, st, tail), ctl=ctl, battery=bat)
    for k in BAT_CRIT_FLAGS:
        if fwl.get(k.lower(), "").lower() == "yes":
            add_issue(res, "CRITICAL", "c%d|%s|%s" % (c, kind, k),
                      "Kontroler c%d: %s zgłasza '%s' - WYMIEŃ BATERIĘ%s" % (c, kind, k, tail),
                      ctl=ctl, battery=bat)
    for k in BAT_WARN_FLAGS:
        if fwl.get(k.lower(), "").lower() == "yes":
            add_issue(res, "WARNING", "c%d|%s|%s" % (c, kind, k),
                      "Kontroler c%d: %s zgłasza '%s'" % (c, kind, k), ctl=ctl, battery=bat)
    for k in BAT_OK_STATUS:
        v = fwl.get(k.lower(), "")
        if v and v.upper() != "OK":
            add_issue(res, "WARNING", "c%d|%s|%s" % (c, kind, k),
                      "Kontroler c%d: %s - '%s' = %s (oczekiwane OK)" % (c, kind, k, v),
                      ctl=ctl, battery=bat)
    for k in ("Over Temperature", "Over Charged"):
        if gasl.get(k.lower(), "").lower() == "yes":
            add_issue(res, "WARNING", "c%d|%s|%s" % (c, kind, k),
                      "Kontroler c%d: %s zgłasza '%s'" % (c, kind, k), ctl=ctl, battery=bat)
    if gasl.get("is soh good", "").lower() == "no":
        add_issue(res, "WARNING", "c%d|%s|soh" % (c, kind),
                  "Kontroler c%d: %s zgłasza zły stan zdrowia baterii (SOH)" % (c, kind), ctl=ctl, battery=bat)
    t = info.get("Temp") or info.get("Temperature")
    if t not in (None, "") and re.search(r"\d", str(t)):
        t = toint(re.sub(r"\(.*", "", str(t)))
        if t >= BBU_TEMP_CRIT:
            add_issue(res, "CRITICAL", "c%d|%s|temp" % (c, kind),
                      "Kontroler c%d: %s temperatura %d°C (próg krytyczny %d°C)" % (c, kind, t, BBU_TEMP_CRIT),
                      ctl=ctl, battery=bat)
        elif t >= BBU_TEMP_WARN:
            add_issue(res, "WARNING", "c%d|%s|temp" % (c, kind),
                      "Kontroler c%d: %s wysoka temperatura %d°C (próg %d°C)" % (c, kind, t, BBU_TEMP_WARN),
                      ctl=ctl, battery=bat)


def evaluate_drive(res, ctl, d, history, now):
    sid = d["sid"]
    ident = "Dysk %s (S/N %s)" % (d["slot"] or d["path"], d["sn"])

    if d["state"] in PD_CRIT_STATES:
        add_issue(res, "CRITICAL", "%s|state" % sid,
                  "%s: stan %s - dysk uszkodzony/wyłączony z macierzy" % (ident, d["state"]), drive=d)
    elif d["state"] not in PD_OK_STATES:
        add_issue(res, "WARNING", "%s|state" % sid,
                  "%s: stan %s (operacja w toku, np. rebuild/copyback, lub stan nietypowy)"
                  % (ident, d["state"]), drive=d)
    if d["smart_alert"].lower() == "yes":
        add_issue(res, "CRITICAL", "%s|smart_alert" % sid,
                  "%s: dysk zgłasza alarm S.M.A.R.T. (przewiduje własną awarię)" % ident, drive=d)
    if d["pred_fail"] > 0:
        add_issue(res, "CRITICAL", "%s|predfail" % sid,
                  "%s: Predictive Failure Count = %d - kontroler przewiduje awarię dysku"
                  % (ident, d["pred_fail"]), drive=d)

    # liczniki raportowane przez kontroler - z oceną trendu
    check_counter(res, d, sid, ident, "media_err", "Media Error Count", d["media_err"], history, now,
                  abs_warn=MEDIA_ERR_ABS_WARN, abs_crit=MEDIA_ERR_ABS_CRIT,
                  growth_warn=1, fast_growth=MEDIA_ERR_FAST_GROWTH)
    check_counter(res, d, sid, ident, "other_err", "Other Error Count", d["other_err"], history, now,
                  growth_warn=OTHER_ERR_GROWTH_WARN, watch=False)

    # --- SMART przez smartctl ---
    s = None
    if USE_SMARTCTL and d["did"] is not None and d["state"] in ("Onln", "UGood", "GHS", "DHS", "JBOD", "Rbld"):
        s = smart_info(d["did"], d["model"])
        d["smart"] = s
        if d["temp"] is None and s.get("temp") is not None:
            d["temp"] = s["temp"]
    if s and s["available"]:
        if s["health"] is False:
            add_issue(res, "CRITICAL", "%s|smart_health" % sid,
                      "%s: smartctl - ogólna ocena zdrowia: FAILED" % ident, drive=d)
        for f in s["failing"]:
            add_issue(res, "CRITICAL", "%s|smart_attr_%s" % (sid, f.split()[0]),
                      "%s: atrybut SMART poniżej progu: %s" % (ident, f), drive=d)

        if s["life"] is not None and d["med"] == "SSD":
            hist = [[r["t"], r["drives"][sid]["life"]] for r in history
                    if sid in r.get("drives", {}) and r["drives"][sid].get("life") is not None]
            est = estimate_days(s["life"], s["poh"], hist, now)
            if est:
                d["eta_days"], d["eta_basis"] = est
                if est[0] <= SSD_DAYS_CRIT:
                    add_issue(res, "CRITICAL", "%s|eta" % sid,
                              "%s: PROGNOZA - SSD wyczerpie zapas zapisów za ok. %d dni (%s)"
                              % (ident, est[0], est[1]), drive=d)
                elif est[0] <= SSD_DAYS_WARN:
                    add_issue(res, "WARNING", "%s|eta" % sid,
                              "%s: PROGNOZA - SSD wyczerpie zapas zapisów za ok. %d dni (%s)"
                              % (ident, est[0], est[1]), drive=d)
            if s["life"] <= SSD_LIFE_CRIT:
                add_issue(res, "CRITICAL", "%s|life" % sid,
                          "%s: SSD prawie zużyty - pozostało ok. %d%% zapasu zapisów flash (próg %d%%)"
                          % (ident, s["life"], SSD_LIFE_CRIT), drive=d)
            elif s["life"] <= SSD_LIFE_WARN:
                add_issue(res, "WARNING", "%s|life" % sid,
                          "%s: SSD się zużywa - pozostało ok. %d%% zapasu zapisów flash (próg %d%%)"
                          % (ident, s["life"], SSD_LIFE_WARN), drive=d)
        if s["spare"] is not None and s["spare"] <= SPARE_WARN:
            add_issue(res, "WARNING", "%s|spare" % sid,
                      "%s: kończy się rezerwa komórek SSD (%d%%)" % (ident, s["spare"]), drive=d)

        for name, raw in s["defects"].items():
            field = "smart_" + name
            if name in UNCORRECTABLE_ATTRS:
                check_counter(res, d, sid, ident, field, "SMART " + name, raw, history, now,
                              abs_warn=1, fast_growth=SMART_UNCORR_FAST_GROWTH)
            elif name.startswith("Reallocated") and d["med"] != "SSD":
                check_counter(res, d, sid, ident, field, "SMART " + name, raw, history, now,
                              abs_warn=HDD_REALLOC_ABS_WARN, fast_growth=SMART_FAST_GROWTH)
            else:
                check_counter(res, d, sid, ident, field, "SMART " + name, raw, history, now,
                              fast_growth=SMART_FAST_GROWTH)
        if s["grown_defects"] is not None:
            check_counter(res, d, sid, ident, "grown", "grown defect list", s["grown_defects"], history, now,
                          abs_warn=GROWN_ABS_WARN, abs_crit=GROWN_ABS_CRIT,
                          growth_warn=GROWN_GROWTH_WARN, fast_growth=GROWN_FAST_GROWTH)

    media = d["med"] if d["med"] in ("SSD", "HDD") else "HDD"
    if d["temp"] is not None:
        if d["temp"] >= TEMP_CRIT[media]:
            add_issue(res, "CRITICAL", "%s|temp" % sid,
                      "%s: temperatura %d°C (próg krytyczny %d°C)" % (ident, d["temp"], TEMP_CRIT[media]), drive=d)
        elif d["temp"] >= TEMP_WARN[media]:
            add_issue(res, "WARNING", "%s|temp" % sid,
                      "%s: wysoka temperatura %d°C (próg %d°C)" % (ident, d["temp"], TEMP_WARN[media]), drive=d)


def evaluate(res, history, now):
    for ctl in res["controllers"]:
        c = ctl["id"]
        if ctl["status"] not in ("Optimal", "?"):
            add_issue(res, "CRITICAL" if ctl["status"] in ("Failed", "Offline") else "WARNING",
                      "c%d|ctrl_status" % c,
                      "Kontroler c%d (%s) ma stan: %s" % (c, ctl["model"], ctl["status"]), ctl=ctl)
        if ctl.get("mem_unc", 0) > 0:
            add_issue(res, "CRITICAL", "c%d|mem_unc" % c,
                      "Kontroler c%d: niekorygowalne błędy pamięci cache (%d)" % (c, ctl["mem_unc"]), ctl=ctl)
        for bat in ctl["battery"]:
            try:
                evaluate_battery(res, ctl, bat)
            except Exception as e:   # błąd w interpretacji baterii nie może zablokować monitoringu dysków
                add_issue(res, "WARNING", "c%d|%s|parse" % (c, bat["kind"]),
                          "Kontroler c%d: nie udało się zinterpretować danych %s (%s: %s)"
                          % (c, bat["kind"], type(e).__name__, e), ctl=ctl)

        # --- macierze ---
        for vd in ctl["vds"]:
            state = str(vd.get("State", "?")).strip()
            dgvd = str(vd.get("DG/VD", "?"))
            dg = dgvd.split("/")[0]
            members = [d for d in ctl["drives"] if d["dg"] == dg]
            vd["members"] = members
            if state == "Optl":
                vd["sev"] = "OK"
                continue
            if state in ("Dgrd", "Pdgd", "OfLn"):
                sev = "CRITICAL"
                why = {"Dgrd": "DEGRADED - utrata redundancji",
                       "Pdgd": "PARTIALLY DEGRADED - utrata redundancji",
                       "OfLn": "OFFLINE - macierz niedostępna"}[state]
            else:
                sev, why = "WARNING", "nietypowy stan"
            bad = [d for d in members if d["state"] not in PD_OK_STATES]
            extra = ""
            if bad:
                extra = " Dyski problematyczne: " + ", ".join(
                    "%s [S/N %s, stan %s]" % (d["slot"], d["sn"], d["state"]) for d in bad)
            vd["sev"] = sev
            add_issue(res, sev, "c%d|vd%s|state" % (c, dgvd.replace("/", "_")),
                      "Macierz c%d VD %s (%s %s, %s): stan %s - %s.%s"
                      % (c, dgvd, vd.get("TYPE", "?"), vd.get("Size", "?"), vd.get("Name") or "-",
                         state, why, extra), vd=vd, ctl=ctl)

        for d in ctl["drives"]:
            evaluate_drive(res, ctl, d, history, now)

    sevs = [i["sev"] for i in res["issues"] if SEV_RANK[i["sev"]] > 0]
    res["severity"] = max(sevs, key=lambda x: SEV_RANK[x]) if sevs else "OK"


# -----------------------------------------------------------------------------
# interpretacja problemów (co to znaczy / co zrobić)
# -----------------------------------------------------------------------------

FIELD_INFO = {
    "media_err": "Błędy nośnika (Media Error Count): dysk nie potrafił poprawnie odczytać lub zapisać danych "
                 "z komórek flash (SSD) albo z talerza (HDD).",
    "other_err": "Błędy komunikacji dysku z kontrolerem (Other Error Count): resety łącza SAS/SATA, problemy "
                 "z backplane lub złączem. Niekoniecznie wina samego dysku.",
    "grown": "Lista uszkodzonych sektorów (grown defect list): sektory, które dysk uznał za zepsute i zastąpił "
             "sektorami z puli zapasowej. Stała liczba = dysk się ustabilizował, rosnąca = powierzchnia talerza się sypie.",
}
SMART_ATTR_INFO = {
    "Reallocated_Sector_Ct": "Przealokowane sektory: dysk przeniósł zepsute sektory do rezerwy.",
    "Reallocated_Event_Count": "Zdarzenia przealokowania sektorów: ile razy dysk przenosił zepsute sektory do rezerwy.",
    "Current_Pending_Sector": "Sektory oczekujące: dysk nie mógł ich odczytać i czeka na ponowny zapis/realokację; "
                              "dane w tych miejscach mogą być już nieczytelne.",
    "Offline_Uncorrectable": "Sektory nieodczytywalne wykryte w teście offline - błędów nie dało się skorygować.",
    "Reported_Uncorrect": "Błędy odczytu, których dysk nie zdołał skorygować - kontroler dostał z dysku błąd zamiast danych.",
    "Uncorrectable_Error_Cnt": "Błędy, których dysk nie zdołał skorygować.",
    "Program_Fail_Count": "SSD: nieudane zapisy do komórek flash - objaw zużycia lub uszkodzenia pamięci.",
    "Erase_Fail_Count": "SSD: nieudane kasowania bloków flash - objaw zużycia lub uszkodzenia pamięci.",
    "Program_Fail_Cnt_Total": "SSD: nieudane zapisy do komórek flash - objaw zużycia lub uszkodzenia pamięci.",
    "Erase_Fail_Count_Total": "SSD: nieudane kasowania bloków flash - objaw zużycia lub uszkodzenia pamięci.",
    "Grown_Bad_Blocks": "SSD: bloki flash, które padły w trakcie pracy i zostały wyłączone z użycia.",
    "Runtime_Bad_Block": "SSD: bloki flash, które padły w trakcie pracy.",
}

EXPLAIN = []


def _ex(rx, meaning, action):
    EXPLAIN.append((re.compile(rx), meaning, action))


def _counter_explain(m, i):
    field, kind = m.group("field"), m.group("kind")
    what = FIELD_INFO.get(field) or SMART_ATTR_INFO.get(field[6:], "Licznik SMART dysku (%s)." % field[6:])
    if kind in ("incr", "fast"):
        return (what + " Licznik ROŚNIE - to oznaka postępującej degradacji dysku, a nie jednorazowego zdarzenia.",
                "Przygotuj dysk zamienny i zaplanuj wymianę" + (" - wzrost jest szybki, nie zwlekaj" if kind == "fast" else "")
                + ". W macierzy z redundancją (RAID1/5/6) wymiana dysku odbywa się bez przestoju. "
                  "Śledź zmiany poleceniem: raid_monitor.py trend")
    if kind == "abs":
        return (what + " Wartość przekroczyła próg ostrzegawczy lub krytyczny.",
                "Sprawdź, czy licznik rośnie (raid_monitor.py trend). Jeśli tak - wymień dysk; jeśli nie - obserwuj.")
    if kind == "stable":
        return (what + " Wartość jest niezerowa, ale nie zmienia się od co najmniej %d dni, więc dysk wygląda na stabilny." % STABLE_DAYS,
                "Nic nie trzeba robić. Skrypt zaalarmuje, jeśli licznik zacznie rosnąć.")
    return (what + " Licznik jest niezerowy, ale skrypt ma jeszcze za krótką historię (mniej niż %d dni), by ocenić, czy rośnie." % STABLE_DAYS,
            "Nic nie trzeba robić. Skrypt sam oceni trend; alarm pojawi się, jeśli licznik zacznie rosnąć.")


def _vd_explain(m, i):
    if "OFFLINE" in i["text"]:
        return ("Macierz jest niedostępna - maszyny i dane na tym wolumenie nie działają.",
                "Natychmiast sprawdź dyski macierzy (raport poniżej), nie wyłączaj serwera i skontaktuj się z serwisem; "
                "przygotuj odtworzenie z kopii zapasowej.")
    return ("Macierz nadal działa, ale straciła redundancję: kolejna awaria dysku (w RAID1/RAID5 jedna, w RAID6 druga) "
            "może oznaczać utratę danych.",
            "Jak najszybciej wymień wskazany dysk (numer slotu i S/N podane w problemie). Po wymianie kontroler sam "
            "uruchomi rebuild - sprawdzaj postęp: perccli64 /c0/eall/sall show rebuild. Do końca rebuildu nie obciążaj "
            "macierzy i miej aktualną kopię zapasową.")


def _drive_state_explain(m, i):
    t = i["text"]
    if "Rbld" in t:
        return ("Dysk jest odbudowywany (rebuild) - macierz odtwarza na nim dane po awarii innego dysku. "
                "W tym czasie wydajność jest niższa, a redundancja ograniczona.",
                "Poczekaj do końca rebuildu (postęp: perccli64 /c0/eall/sall show rebuild). Nie wyjmuj dysków.")
    if any(s in t for s in PD_CRIT_STATES):
        return ("Dysk został uznany za uszkodzony albo wyłączony z macierzy; macierz pracuje bez niego (jeśli ma redundancję).",
                "Wymień dysk na taki sam lub większy (numer slotu i S/N w opisie problemu). Kontroler po wymianie "
                "rozpocznie rebuild. Jeśli dysk został wyłączony przez przypadek (np. po wyjęciu), sprawdź konfigurację "
                "(perccli64 /c0/fall show).")
    return ("Dysk jest w stanie innym niż normalny (np. trwa kopiowanie, czyszczenie lub inna operacja kontrolera).",
            "Sprawdź stan poleceniem perccli64 /c0/eall/sall show; zwykle wystarczy poczekać na koniec operacji.")


_ex(r"^tool\|failure$",
    "Skrypt nie mógł odczytać stanu RAID, więc nie wiadomo, czy macierze i dyski są zdrowe.",
    "Sprawdź na serwerze: czy działa perccli64 show, czy ścieżka PERCCLI jest poprawna, czy skrypt działa jako root "
    "i czy kontroler jest widoczny (lspci | grep -i raid).")
_ex(r"\|parse$",
    "Skrypt nie potrafił zinterpretować danych z kontrolera (inny format odpowiedzi perccli).",
    "Uruchom raid_monitor.py dump i prześlij wynik autorowi skryptu, aby dostosować parser.")
_ex(r"^c\d+\|ctrl_status$",
    "Kontroler RAID zgłasza stan inny niż Optimal - może to wpływać na wszystkie macierze.",
    "Sprawdź log kontrolera: perccli64 /c0 show eventloginfo oraz perccli64 /c0 show all.")
_ex(r"^c\d+\|mem_unc$",
    "Pamięć cache kontrolera ma niekorygowalne błędy - dane przechodzące przez cache mogą być uszkodzone.",
    "Zaplanuj pilną wymianę kontrolera lub modułu pamięci cache; zrób kopię zapasową danych.")
_ex(r"^c\d+\|vd\d+_\d+\|state$", _vd_explain, None)
_ex(r"^c\d+\|(?:BBU|CacheVault)\|",
    "Bateria BBU/CacheVault podtrzymuje pamięć cache kontrolera na wypadek zaniku zasilania. Gdy jest niesprawna, "
    "kontroler wyłącza zapis z buforem (write-back przechodzi na write-through), co spowalnia dyski, a przy wymuszonym "
    "write-back grozi utratą danych z cache.",
    "Zaplanuj wymianę baterii (zwykle po 2-3 latach pracy). Do czasu wymiany sprawdź zasady cache wolumenów: "
    "perccli64 /c0/vall show (kolumna Cache).")
_ex(r"\|state$", _drive_state_explain, None)
_ex(r"\|smart_alert$",
    "Dysk sam przewiduje swoją awarię (alarm S.M.A.R.T.). Producent ustawia go, gdy wskaźniki zdrowia przekroczą próg - "
    "awaria bywa kwestią dni lub tygodni.",
    "Wymień dysk w najbliższym możliwym terminie. Wcześniej upewnij się, że macierz jest sprawna (Optl) i istnieje kopia zapasowa.")
_ex(r"\|predfail$",
    "Kontroler odnotował zdarzenia predykcji awarii dysku (Predictive Failure Count) - ostrzeżenie przed awarią.",
    "Wymień dysk w najbliższym możliwym terminie; sprawdź kopię zapasową.")
_ex(r"\|smart_health$",
    "Ogólna ocena zdrowia SMART wyszła negatywna (FAILED) - dysk sam uznaje, że jest uszkodzony.",
    "Wymień dysk jak najszybciej.")
_ex(r"\|smart_attr_",
    "Wskaźnik SMART spadł poniżej progu ustawionego przez producenta dysku.",
    "Traktuj dysk jako zagrożony: wymień go w najbliższym możliwym terminie.")
_ex(r"\|(?P<field>media_err|other_err|grown|smart_\w+?)_(?P<kind>abs|fast|incr|obs|stable)$", _counter_explain, None)
_ex(r"\|temp$",
    "Dysk jest zbyt gorący. Wysoka temperatura skraca życie dysku, a SSD może ograniczać wydajność (throttling).",
    "Sprawdź wentylatory, drożność przepływu powietrza i filtry kurzu w serwerze oraz temperaturę w szafie.")
_ex(r"\|life$",
    "SSD wykorzystał większość zapasu zapisów komórek flash. Po jego wyczerpaniu dysk przestaje przyjmować zapisy "
    "(tryb tylko do odczytu) albo zaczyna gubić dane.",
    "Zamów i wymień dysk, najlepiej zanim zapas spadnie poniżej 10%. W RAID z redundancją wymiana nie wymaga przestoju.")
_ex(r"\|eta$",
    "Przy obecnym tempie zapisów zapas komórek flash tego SSD skończy się w podanym czasie. To szacunek zużycia, "
    "nie prognoza losowej awarii elektroniki dysku.",
    "Zaplanuj wymianę przed tym terminem. Jeśli to możliwe, zmniejsz zapisy na tej macierzy (logi, swap, cache).")
_ex(r"\|spare$",
    "Kończy się rezerwa komórek zamiennych SSD; po jej wyczerpaniu kolejne uszkodzone komórki oznaczają utratę pojemności lub danych.",
    "Zaplanuj wymianę dysku.")


def explain(issue):
    for rx, meaning, action in EXPLAIN:
        m = rx.search(issue["key"])
        if m:
            if callable(meaning):
                return meaning(m, issue)
            return meaning, action
    return None


# -----------------------------------------------------------------------------
# raport tekstowy
# -----------------------------------------------------------------------------

def bat_summary(bat):
    i, cap, fw, gas = bat["info"], bat["cap"], bat["fw"], bat["gas"]
    parts = ["stan %s" % (i.get("State") or i.get("Battery State") or "?")]
    if i.get("Model"):
        parts.append("model %s" % i["Model"])
    t = i.get("Temp") or i.get("Temperature")
    if t:
        parts.append("temp %s" % str(t).split("(")[0].strip())
    if i.get("Voltage"):
        parts.append("napięcie %s" % i["Voltage"])
    charge = cap.get("Relative State of Charge") or gas.get("Relative State of Charge")
    if charge:
        parts.append("naładowanie %s" % charge)
    if cap.get("Remaining Capacity") and cap.get("Full Charge Capacity"):
        parts.append("pojemność %s / %s" % (cap["Remaining Capacity"], cap["Full Charge Capacity"]))
    if cap.get("Cycle Count") not in (None, ""):
        parts.append("cykle %s" % cap["Cycle Count"])
    if gas.get("Is SOH Good"):
        parts.append("SOH dobre: %s" % gas["Is SOH Good"])
    if i.get("Mode"):
        parts.append("tryb %s" % i["Mode"])
    if i.get("Next Learn"):
        parts.append("następny learn %s" % i["Next Learn"])
    if i.get("MfgDate"):
        parts.append("data prod. %s" % i["MfgDate"])
    if i.get("RetentionTime"):
        parts.append("podtrzymanie %s" % i["RetentionTime"])
    if str(fw.get("Replacement required", "")).lower() == "yes":
        parts.append("WYMAGANA WYMIANA")
    return "  %s: %s" % (bat["kind"], ", ".join(parts))


def drive_sev(d):
    sevs = [s for s, _ in d["issues"] if SEV_RANK[s] > 0]
    if sevs:
        return max(sevs, key=lambda x: SEV_RANK[x])
    return "OK (info)" if d["issues"] else "OK"


def drive_ident(d):
    lines = ["  Lokalizacja : kontroler c%d, enclosure:slot %s (ścieżka %s)" % (d["ctrl"], d["slot"], d["path"]),
             "  Device ID   : %s" % d["did"],
             "  Model       : %s (%s, %s, %s)" % (d["model"], d["med"], d["intf"], d["size"]),
             "  Nr seryjny  : %s" % d["sn"],
             "  Firmware    : %s" % d["fw"],
             "  Stan / DG   : %s / %s" % (d["state"], d["dg"]),
             "  Błędy       : media=%d other=%d predictive=%d, alarm SMART dysku=%s"
             % (d["media_err"], d["other_err"], d["pred_fail"],
                "TAK" if d["smart_alert"].lower() == "yes" else "brak")]
    if d["temp"] is not None:
        lines.append("  Temperatura : %d°C" % d["temp"])
    s = d.get("smart")
    if s and s["available"]:
        lines.append("  smartctl    : zdrowie=%s, pozostały zapas zapisów SSD=%s, rezerwa=%s"
                     % ({True: "OK", False: "FAILED", None: "?"}[s["health"]],
                        "%d%%" % s["life"] if s["life"] is not None else "n/d",
                        "%d%%" % s["spare"] if s["spare"] is not None else "n/d"))
        if d.get("eta_days") is not None:
            lines.append("  Do wyczerpania: %s (szacunek wg: %s; to czas zużycia komórek flash, nie prognoza losowej awarii)"
                         % (eta_text(d), d["eta_basis"]))
    elif s:
        lines.append("  smartctl    : niedostępny (%s)" % s["note"])
    for t in d["trend"]:
        lines.append("  Trend       : " + t)
    return "\n".join(lines)


def _wrap(prefix, text, width=100):
    return textwrap.fill(text, width=width, initial_indent=prefix,
                         subsequent_indent=" " * len(prefix))


def format_issues(res):
    """Lista problemów z interpretacją (co to znaczy / co zrobić)."""
    real = [i for i in res["issues"] if SEV_RANK[i["sev"]] > 0]
    infos = [i for i in res["issues"] if SEV_RANK[i["sev"]] == 0]
    out = []
    if real:
        ncrit = sum(1 for i in real if i["sev"] == "CRITICAL")
        nwarn = len(real) - ncrit
        out.append("WYKRYTE PROBLEMY: %d krytycznych (wymagają szybkiej reakcji), %d ostrzeżeń (zaplanuj / obserwuj)"
                   % (ncrit, nwarn))
        for i in sorted(real, key=lambda x: -SEV_RANK[x["sev"]]):
            out.append("")
            out.append(_wrap(" [%s] " % i["sev"], i["text"]))
            ex = explain(i)
            if ex:
                out.append(_wrap("      Co to znaczy : ", ex[0]))
                out.append(_wrap("      Co zrobić    : ", ex[1]))
    else:
        out.append("Brak problemów wymagających reakcji - macierze i dyski w porządku.")
    if infos:
        out.append("")
        out.append("INFORMACJE (bez alarmu):")
        for i in infos:
            out.append(_wrap("  - ", i["text"]))
            ex = explain(i)
            if ex:
                out.append(_wrap("      Co to znaczy : ", ex[0]))
    return "\n".join(out)


def format_report(res, with_issues=True):
    out = []
    out.append("Raport RAID  host: %s  czas: %s" % (HOST, res["time"].strftime("%Y-%m-%d %H:%M:%S")))
    out.append("Ocena ogólna: %s" % res["severity"])
    if res.get("hist_info"):
        out.append(res["hist_info"])
    out.append("=" * 100)
    for ctl in res["controllers"]:
        out.append("")
        out.append("KONTROLER c%d: %s  S/N %s  FW %s  status: %s"
                   % (ctl["id"], ctl["model"], ctl["serial"], ctl["fw"], ctl["status"]))
        if not ctl["battery"]:
            out.append("  Bateria/CacheVault: brak (lub niewidoczna dla perccli)")
        for bat in ctl["battery"]:
            out.append(bat_summary(bat))
        out.append("")
        out.append("  MACIERZE I DYSKI (pogrupowane wg macierzy):")
        if not ctl["vds"]:
            out.append("    (brak skonfigurowanych macierzy)")
        fmt = "    %-7s %-4s %-7s %-3s %-10s %-26s %-20s %-5s %-11s %-11s %-12s %-15s %s"
        hdr = fmt % ("Slot", "DID", "Stan", "Typ", "Rozmiar", "Model", "Nr seryjny",
                     "Temp", "Err M/O/P", "Alarm SMART", "SSD zostało", "Do wyczerpania", "Ocena")

        def drive_row(d):
            s = d.get("smart")
            life = "%d%%" % s["life"] if s and s["life"] is not None else "-"
            return fmt % (d["slot"], d["did"], d["state"], d["med"], d["size"],
                          d["model"][:26], d["sn"],
                          "%dC" % d["temp"] if d["temp"] is not None else "-",
                          "%d/%d/%d" % (d["media_err"], d["other_err"], d["pred_fail"]),
                          "TAK" if d["smart_alert"].lower() == "yes" else "brak",
                          life, eta_text(d), drive_sev(d))

        used = set()
        for vd in ctl["vds"]:
            dgvd = str(vd.get("DG/VD") or "")
            dg = dgvd.split("/")[0].strip()
            members = [d for d in ctl["drives"] if str(d["dg"]).strip() == dg and dg not in ("", "-")]
            for d in members:
                used.add(id(d))
            out.append("")
            out.append("    === VD %s  %s  \"%s\"  stan: %s  rozmiar: %s  cache: %s  dysków: %d  -> %s ==="
                       % (dgvd, vd.get("TYPE"), vd.get("Name") or "-", vd.get("State"),
                          vd.get("Size"), vd.get("Cache"), len(members), vd.get("sev", "OK")))
            out.append(hdr)
            for d in members:
                out.append(drive_row(d))
        rest = [d for d in ctl["drives"] if id(d) not in used]
        if rest:
            out.append("")
            out.append("    === Dyski poza macierzami (hot spare / unconfigured / foreign) ===")
            out.append(hdr)
            for d in rest:
                out.append(drive_row(d))
        out.append("")
        out.append("  Legenda:")
        out.append("   Err M/O/P    = błędy dysku: Media / Other / Predictive Failure (zgłaszane przez kontroler)")
        out.append("   Alarm SMART  = brak: dysk nie zgłasza własnej prognozy awarii (dobrze); TAK: dysk przewiduje awarię")
        out.append("   SSD zostało  = szacowany pozostały zapas zapisów komórek flash SSD (100% = nowy, 0% = wyczerpany);")
        out.append("                  '-' = dysk mechaniczny (HDD) lub brak danych SMART")
        out.append("   Do wyczerpania = szacunek, za ile dni SSD wyczerpie zapas zapisów przy obecnym tempie zużycia")
        out.append("                  (to nie jest prognoza losowej awarii elektroniki; dokładność rośnie z czasem pracy skryptu)")
        out.append("   Ocena        = OK (info): licznik niezerowy, ale bez wzrostu - szczegóły w sekcji INFORMACJE")
    out.append("")
    out.append("=" * 100)
    if with_issues:
        out.append(format_issues(res))
    return "\n".join(out)


def format_mail_body(res, kind):
    lines = []
    if kind == "ALERT":
        lines.append("UWAGA: skrypt monitorujący wykrył problemy z RAID/dyskami na serwerze %s." % HOST)
    elif kind == "RECOVERY":
        lines.append("Problemy RAID/dysków zgłaszane wcześniej na serwerze %s już nie występują." % HOST)
    else:
        lines.append("Mail testowy ze skryptu monitorującego RAID na serwerze %s." % HOST)
    lines.append("")
    real = [i for i in res["issues"] if SEV_RANK[i["sev"]] > 0]
    lines.append(format_issues(res))
    lines.append("")
    if real:
        shown = set()
        drives = [i["drive"] for i in real if i["drive"] is not None]
        for vd in [i["vd"] for i in real if i["vd"] is not None]:
            drives.extend(vd.get("members", []))
        bats = []
        for i in real:
            if i.get("battery") is not None and i["battery"] not in bats:
                bats.append(i["battery"])
                ctl = i["ctl"]
                lines.append("BATERIA PODTRZYMANIA CACHE (kontroler c%d: %s, S/N %s):" % (ctl["id"], ctl["model"], ctl["serial"]))
                lines.append(bat_summary(i["battery"]))
                lines.append("")
        if drives:
            lines.append("DYSKI, KTÓRYCH DOTYCZY PROBLEM (identyfikacja):")
            for d in drives:
                if d["path"] in shown:
                    continue
                shown.add(d["path"])
                lines.append("")
                lines.append(" Dysk %s:" % d["slot"])
                lines.append(drive_ident(d))
                for sev, t in d["issues"]:
                    if SEV_RANK[sev] > 0:
                        lines.append("  -> [%s] %s" % (sev, t))
            lines.append("")
    lines.append("-" * 80)
    lines.append("PEŁNY RAPORT:")
    lines.append("")
    lines.append(format_report(res, with_issues=False))
    return "\n".join(lines)


def fmt_delta(x):
    return "n/d" if x is None else "%+d" % x[0]


def print_trend(res, history, now):
    print("Trend liczników dysków (zmiana względem historii z STATE_DIR=%s)" % STATE_DIR)
    print(res.get("hist_info") or "Brak historii.")
    print("Historia powstaje przy uruchomieniach z crona (tryb monitor).")
    print("")
    fields = [("media_err", "Media Error"), ("other_err", "Other Error"), ("grown", "Grown defects")]
    for ctl in res["controllers"]:
        for d in ctl["drives"]:
            sid = d["sid"]
            cur = {"media_err": d["media_err"], "other_err": d["other_err"]}
            s = d.get("smart")
            names = list(fields)
            if s and s["available"]:
                cur["grown"] = s["grown_defects"]
                cur["life"] = s["life"]
                names.append(("life", "SSD zostało %"))
                for k, v in s["defects"].items():
                    cur["smart_" + k] = v
                    names.append(("smart_" + k, k))
            parts = []
            for f, label in names:
                v = cur.get(f)
                if v is None:
                    continue
                parts.append("%s=%d [1d %s | 7d %s | 30d %s]" % (
                    label, v, fmt_delta(delta_over(history, sid, f, v, now, 1)),
                    fmt_delta(delta_over(history, sid, f, v, now, 7)),
                    fmt_delta(delta_over(history, sid, f, v, now, 30))))
            print("%-6s %-22s %s %s" % (d["slot"], d["sn"], d["med"], drive_sev(d)))
            for p in parts:
                print("         " + p)
    print("")
    print("Odczyt: np. '7d +2' = licznik wzrósł o 2 w ciągu ostatnich 7 dni; 'n/d' = za krótka historia.")


# -----------------------------------------------------------------------------
# mail
# -----------------------------------------------------------------------------

def send_mail(subject, body):
    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(MAIL_TO)
    msg["Subject"] = MAIL_SUBJECT_PREFIX + subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body, charset="utf-8")

    ctx = ssl.create_default_context()
    if not SMTP_VERIFY_TLS:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if SMTP_SECURITY == "ssl":
        s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT, context=ctx)
    else:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
        if SMTP_SECURITY == "starttls":
            s.starttls(context=ctx)
    try:
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg, from_addr=MAIL_FROM, to_addrs=MAIL_TO)
    finally:
        try:
            s.quit()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------

def tool_failure_result(err):
    res = {"controllers": [], "issues": [], "time": datetime.now(), "severity": "CRITICAL"}
    res["issues"].append({"sev": "CRITICAL", "key": "tool|failure",
                          "text": "Nie można odczytać stanu RAID: %s" % err,
                          "drive": None, "vd": None, "ctl": None, "battery": None})
    return res


def history_info(history, now):
    if not history:
        return "Historia: brak zapisanych próbek (trend zacznie być oceniany po pierwszych uruchomieniach z crona)."
    first = history[0]["t"]
    return "Historia: %d próbek z %.0f dni (od %s), katalog %s" % (
        len(history), (now - first) / 86400.0,
        datetime.fromtimestamp(first).strftime("%Y-%m-%d"), STATE_DIR)


def main():
    ap = argparse.ArgumentParser(description="Monitoring RAID (perccli) i predykcja awarii dysków")
    ap.add_argument("mode", nargs="?", choices=["monitor", "test", "dump", "trend"],
                    help="monitor = mail przy problemie + zapis historii, test = zawsze wyślij mail, "
                         "trend = porównanie z historią, dump = surowe dane baterii; brak = sam raport")
    ap.add_argument("--monitor", action="store_true", help="to samo co 'monitor'")
    ap.add_argument("--test", action="store_true", help="to samo co 'test'")
    a = ap.parse_args()
    mode = a.mode or ("monitor" if a.monitor else "test" if a.test else None)
    load_overrides()

    if mode == "dump":      # surowe odpowiedzi perccli o baterii - do diagnostyki parsera
        for sub in ("bbu", "cv"):
            try:
                rc, out = run_cmd([PERCCLI, "/c0/%s" % sub, "show", "all", "J"])
            except ToolError as e:
                out = str(e)
            print("=== perccli /c0/%s show all J ===\n%s\n" % (sub, out))
        return 0

    now = time.time()
    ensure_state_dir()
    state = load_state()
    history = load_history(now)
    try:
        res = collect()
        evaluate(res, history, now)
    except ToolError as e:
        res = tool_failure_result(e)
    res["hist_info"] = history_info(history, now)
    code = {"OK": 0, "WARNING": 1, "CRITICAL": 2}[res["severity"]]

    if mode is None:
        print(format_report(res))
        return code
    if mode == "trend":
        print_trend(res, history, now)
        return 0

    if mode == "test":
        try:
            send_mail("[RAID TEST] %s: stan macierzy i dysków (%s)" % (HOST, res["severity"]),
                      format_mail_body(res, "TEST"))
            print("Mail testowy wysłany do: %s" % ", ".join(MAIL_TO))
        except Exception as e:
            sys.stderr.write("Błąd wysyłki maila: %s\n" % e)
            return 3
        return code

    # --- monitor: zapis historii, decyzja o alarmie na podstawie zmian ---
    if res["controllers"]:
        write_history(build_record(res, now), history, state, now)

    min_rank = SEV_RANK[MIN_SEVERITY_MAIL]
    alerting = [i for i in res["issues"] if SEV_RANK[i["sev"]] >= min_rank]
    fp = sorted("%s|%s" % (i["sev"], i["key"]) for i in alerting)   # eskalacja WARNING->CRITICAL też wyśle mail
    prev_fp = state.get("fingerprint", [])
    last_sent = state.get("last_sent", 0)
    reminder_h = REMINDER_HOURS if res["severity"] == "CRITICAL" else WARNING_REMINDER_HOURS

    send_kind = None
    if alerting:
        if fp != prev_fp or now - last_sent > reminder_h * 3600:
            send_kind = "ALERT"
    elif prev_fp:
        send_kind = "RECOVERY"

    if send_kind:
        try:
            if send_kind == "ALERT":
                subj = "[RAID %s] %s: %s" % (res["severity"], HOST, alerting[0]["text"][:120]
                                             if len(alerting) == 1 else "wykryto %d problemów" % len(alerting))
            else:
                subj = "[RAID OK] %s: problemy ustąpiły" % HOST
            send_mail(subj, format_mail_body(res, send_kind))
            state["last_sent"] = now
            state["fingerprint"] = fp
        except Exception as e:
            sys.stderr.write("Błąd wysyłki maila: %s\n" % e)
            save_state(state)
            return 3
    save_state(state)
    return code


if __name__ == "__main__":
    sys.exit(main())
  
