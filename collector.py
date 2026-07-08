# Sbira PSE snapshoty s casovou znackou. Spousti GitHub Actions kazdych 30 min (+ externi trigger).
# Snima CELY BPKD plan na 3 dny dopredu. KAZDY OBCHODNI DEN -> SVUJ SOUBOR (vsechny revize).
# poeb-rbn (bid stack energie): verzovani po periodach — perioda se ulozi poprve a pak jen kdyz se zmeni.
import requests, os, hashlib
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pandas as pd

BASE = "https://api.raporty.pse.pl/api/"
NOW  = datetime.now(timezone.utc)
SNAP = NOW.strftime("%Y-%m-%d %H:%M:%S")
# business_date u PSE = lokalni kalendarni den; odvozovat z UTC znamenalo nocni slepotu 00:15-02:15
DEN_LOK = NOW.astimezone(ZoneInfo("Europe/Warsaw"))

ENDPOINTY = {
    "BPKD":       ["pdgobpkd", "pdgob"],   # CELY plan koordynacyjny (37 sloupcu)
    "price_fcst": ["price-fcst"],          # prognoza CEN + odchylky (bonus)
}
DNI_DOPREDU = 3                            # dnes + zitra + pozitri

def stahni(endpoint, den):
    out, url = [], f"{BASE}{endpoint}?$filter=business_date eq '{den}'&$first=20000"
    while url:
        try:
            r = requests.get(url, timeout=60)
            if not r.ok: break
            d = r.json(); out.extend(d.get("value", [])); url = d.get("nextLink")
        except Exception: break
    return out

os.makedirs("data", exist_ok=True)
dny = [(DEN_LOK + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DNI_DOPREDU)]

for nazev, varianty in ENDPOINTY.items():
    endpoint = next((ep for ep in varianty if stahni(ep, dny[0])), None)   # najdi funkcni
    if endpoint is None:
        print(f"{nazev}: zadny z {varianty} nevraci data"); continue

    for den in dny:                                       # KAZDY obchodni den ZVLAST
        radky = stahni(endpoint, den)
        if not radky:
            print(f"{nazev} {den}: zatim nepublikovano, preskakuji")   # zadna chyba, jen preskoc
            continue
        df = pd.DataFrame(radky)
        df["snapshot_ts"] = SNAP                          # KLIC: kdy jsme to videli
        soubor = f"data/{nazev}_{den}.parquet"            # NAZEV PODLE OBCHODNIHO DNE (ne dne snimku)
        if os.path.exists(soubor):
            df = pd.concat([pd.read_parquet(soubor), df], ignore_index=True)
        df.to_parquet(soubor, index=False)
        print(f"{nazev} {den} ({endpoint}): +{len(radky)} radku, snapshot {SNAP}, celkem {len(df)}")

# ---------- poeb-rbn: bid stack energie, verzovani po periodach ----------
# Stack periody se publikuje ~2 min po jejim konci a muze byt zpetne REVIDOVAN.
# Plny snapshot celeho dne kazdych 30 min = ~13 MB/den -> neudrzitelne.
# Proto: periodu ulozime poprve, a znovu JEN kdyz se jeji obsah zmeni (revize = signal!).
# Rekonstrukce stavu k casu T = pro kazdou periodu posledni verze se snapshot_ts <= T.

def otisk(df_p):
    """Obsahovy hash stacku jedne periody (nezavisly na poradi radku a na publication_ts)."""
    s = df_p[["ofp", "ofcd", "ofcg"]].astype(float).fillna(-9e9).round(4)
    radky = sorted(map(tuple, s.to_numpy().tolist()))
    return hashlib.md5(repr(radky).encode()).hexdigest()

vcera = (DEN_LOK - timedelta(days=1)).strftime("%Y-%m-%d")
for den in [dny[0], vcera]:   # dnes + vcera (pulnocni dobihani poslednich period a pozdni revize)
    try:
        radky = stahni("poeb-rbn", den)
        if not radky:
            print(f"poeb_rbn {den}: nic nepublikovano"); continue
        novy = pd.DataFrame(radky)
        soubor = f"data/poeb_rbn_{den}.parquet"

        stary, stare_otisky = None, {}
        if os.path.exists(soubor):
            stary = pd.read_parquet(soubor)
            for per, g in stary.groupby("period"):        # posledni ulozena verze kazde periody
                posledni = g[g["snapshot_ts"] == g["snapshot_ts"].max()]
                stare_otisky[per] = otisk(posledni)

        zmenene = [g for per, g in novy.groupby("period") if stare_otisky.get(per) != otisk(g)]
        if not zmenene:
            print(f"poeb_rbn {den}: beze zmen ({novy['period'].nunique()} period beze zmeny)")
            continue
        pridat = pd.concat(zmenene, ignore_index=True)
        pridat["snapshot_ts"] = SNAP
        vysledek = pd.concat([stary, pridat], ignore_index=True) if stary is not None else pridat
        vysledek.to_parquet(soubor, index=False)
        novych = sum(1 for g in zmenene if g["period"].iloc[0] not in stare_otisky)
        print(f"poeb_rbn {den}: +{len(pridat)} radku | {novych} novych period, {len(zmenene)-novych} revizi, celkem {len(vysledek)}")
    except Exception as e:
        # pad poeb-rbn NESMI zabit beh — BPKD/price_fcst snapshoty se musi commitnout vzdy
        print(f"poeb_rbn {den}: CHYBA {type(e).__name__}: {e} — pokracuji")

# ---------- verzovane DENNI archivy: prvni otisky a revize ----------
# cen (crb-rozl): CEN + slozky. Prvni print ~D+1 poledne, revizni vlna ~D+4 (19 % dni), ~D+52 (3 %).
#   PSE prepisuje bez historie -> archivujeme KAZDOU verzi dne (H10: korekce EV vsech chvostovych backtestu).
# pwm_rdb (pwm-rdb): intradenni nominace vymen per hranice, updaty ~hodinove vc. budoucich period.
# Den se ulozi znovu JEN kdyz se obsah zmenil (hash bez publication_ts).

def otisk_dne(df):
    ignoruj = {"publication_ts", "publication_ts_utc", "snapshot_ts"}
    s = df[[c for c in df.columns if c not in ignoruj]].copy()
    for c in s.columns:
        if pd.api.types.is_numeric_dtype(s[c]):
            s[c] = s[c].astype(float).round(4)
    return hashlib.md5(repr(sorted(map(str, s.to_numpy().tolist()))).encode()).hexdigest()

ARCHIVY = {
    "cen":     ("crb-rozl", [(DEN_LOK - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 7)]),
    "pwm_rdb": ("pwm-rdb",  [(DEN_LOK + timedelta(days=i)).strftime("%Y-%m-%d") for i in (-1, 0, 1)]),
    # brana nastupu curtailmentu prosla 8.7.; 30min rozliseni sberu ponechano
    "poze_redoze": ("poze-redoze", [(DEN_LOK + timedelta(days=i)).strftime("%Y-%m-%d") for i in (-1, 0, 1)]),
}
for nazev, (endpoint, dny_arch) in ARCHIVY.items():
    for den in dny_arch:
        try:
            radky = stahni(endpoint, den)
            if not radky:
                continue                                   # den jeste nepublikovan — zadna zprava
            novy = pd.DataFrame(radky)
            soubor = f"data/{nazev}_{den}.parquet"
            stary = None
            if os.path.exists(soubor):
                stary = pd.read_parquet(soubor)
                posledni = stary[stary["snapshot_ts"] == stary["snapshot_ts"].max()]
                if otisk_dne(posledni) == otisk_dne(novy):
                    continue                               # beze zmen — neukladat, nelogovat (6 dni x 48 behu)
            novy["snapshot_ts"] = SNAP
            vysledek = pd.concat([stary, novy], ignore_index=True) if stary is not None else novy
            vysledek.to_parquet(soubor, index=False)
            verze = len(vysledek) // max(len(novy), 1)
            print(f"{nazev} {den}: {'REVIZE' if stary is not None else 'prvni otisk'} (+{len(novy)} radku, verzi {verze})")
        except Exception as e:
            print(f"{nazev} {den}: CHYBA {type(e).__name__}: {e} — pokracuji")

# ---------- doplnkova aukce moci (cmbu-tu ceny, mbu-tu objemy): verzovani po periodach ----------
# H2 KROK 0 overen zivte 2.7.: clearing per 15 min, publikace +1,1-1,2 min po KONCI periody (58/58 period
# s unikatnim pub_ts), bezici perioda videt neni. Rychlejsi nez bid stack (+2,1 min).
PER_PERIOD = {"cmbu_tu": "cmbu-tu", "mbu_tu": "mbu-tu"}
for nazev, endpoint in PER_PERIOD.items():
    for den in [dny[0], vcera]:
        try:
            radky = stahni(endpoint, den)
            if not radky:
                continue
            novy = pd.DataFrame(radky)
            soubor = f"data/{nazev}_{den}.parquet"
            stary, stare = None, {}
            if os.path.exists(soubor):
                stary = pd.read_parquet(soubor)
                for per, g in stary.groupby("period"):
                    posl = g[g["snapshot_ts"] == g["snapshot_ts"].max()]
                    stare[per] = otisk_dne(posl)
            zmenene = [g for per, g in novy.groupby("period") if stare.get(per) != otisk_dne(g)]
            if not zmenene:
                continue
            pridat = pd.concat(zmenene, ignore_index=True)
            pridat["snapshot_ts"] = SNAP
            vysledek = pd.concat([stary, pridat], ignore_index=True) if stary is not None else pridat
            vysledek.to_parquet(soubor, index=False)
            print(f"{nazev} {den}: +{len(pridat)} radku ({len(zmenene)} period)")
        except Exception as e:
            print(f"{nazev} {den}: CHYBA {type(e).__name__}: {e} — pokracuji")

# ---------- JAO intradenni alokacni limity (Core ID CCR): verze pred IDA1 a pred IDA2 ----------
# PSE limity pozice PL vuci Evrope existuji ve 3 verzich: day-ahead (core/allocationConstraint),
# IDCCA (publikace ~13:55-14:40 D-1 lokalne, tj. PRED gate IDA1 15:00) a IDCCB (~20:45-22:25 D-1,
# tesne kolem gate IDA2 22:00). import_PL/export_PL jsou znamenkove meze pozice PL:
# zaporny import_PL = dovoz zavreny a vynucen minimalni export (obdobne export_PL).
# Verzovani obsahovym hashem dne (otisk_dne); sloupec id od JAO zahazujeme (technicke cislo radku).
JAO_ID_DATASETY = {"jao_id1": "IDCCA_allocationConstraint", "jao_id2": "IDCCB_allocationConstraint"}
JAO_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
for nazev, dataset in JAO_ID_DATASETY.items():
    for posun in (0, 1):                                   # dnes + zitra (zitrek pribyva odpoledne a vecer)
        den = (DEN_LOK + timedelta(days=posun)).strftime("%Y-%m-%d")
        try:
            pulnoc = (DEN_LOK + timedelta(days=posun)).replace(hour=0, minute=0, second=0, microsecond=0)
            od = pulnoc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            do = (pulnoc + timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            r = requests.get(f"https://publicationtool.jao.eu/coreID/api/data/{dataset}",
                             params={"FromUtc": od, "ToUtc": do}, headers=JAO_UA, timeout=60)
            if not r.ok:
                continue
            radky = r.json().get("data", [])
            if not radky:
                continue                                   # session jeste nepublikovana — zadna zprava
            novy = pd.DataFrame(radky).drop(columns=["id"], errors="ignore")
            soubor = f"data/{nazev}_{den}.parquet"
            stary = None
            if os.path.exists(soubor):
                stary = pd.read_parquet(soubor)
                posledni = stary[stary["snapshot_ts"] == stary["snapshot_ts"].max()]
                if otisk_dne(posledni.drop(columns=["id"], errors="ignore")) == otisk_dne(novy):
                    continue                               # beze zmen — neukladat, nelogovat
            novy["snapshot_ts"] = SNAP
            vysledek = pd.concat([stary, novy], ignore_index=True) if stary is not None else novy
            vysledek.to_parquet(soubor, index=False)
            print(f"{nazev} {den}: {'REVIZE' if stary is not None else 'prvni otisk'} (+{len(novy)} radku)")
        except Exception as e:
            # pad JAO NESMI zabit sber PSE dat — pokracujeme
            print(f"{nazev} {den}: CHYBA {type(e).__name__}: {e} — pokracuji")

# ---------- denni PL intraday statistiky: TGE RDB (CT+IDA, PLN) + Nord Pool kontrakty (EUR) ----------
# Idempotentni: jen vcerejsi den, jen pokud soubor neexistuje -> 48 pokusu denne, zadny novy workflow.
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TGE_SLOUPCE = ["instrument", "trvani_min",
    "ct_min", "ct_max", "ct_vwap", "ct_vol", "ct_vol_kup", "ct_vol_prod",
    "ida1_eur", "ida1_pln", "ida1_vol", "ida1_vol_kup", "ida1_vol_prod",
    "ida2_eur", "ida2_pln", "ida2_vol", "ida2_vol_kup", "ida2_vol_prod",
    "ida3_eur", "ida3_pln", "ida3_vol", "ida3_vol_kup", "ida3_vol_prod",
    "tot_min", "tot_max", "tot_vwap", "tot_vol", "tot_vol_kup", "tot_vol_prod"]

try:
    soubor = f"data/tge_rdb_{vcera}.parquet"
    if not os.path.exists(soubor):
        from bs4 import BeautifulSoup
        dd = datetime.strptime(vcera, "%Y-%m-%d").strftime("%d-%m-%Y")
        r = requests.get(f"https://tge.pl/energia-elektryczna-rdb?dateShow={dd}&dateAction=prev", headers=UA, timeout=120)
        tab = BeautifulSoup(r.text, "lxml").find_all("table")[0]
        radky = [[c.get_text(strip=True) for c in tr.find_all("td")] for tr in tab.find_all("tr")]
        radky = [x for x in radky if len(x) == 29 and "_" in x[0]]
        df = pd.DataFrame(radky, columns=TGE_SLOUPCE)
        for c in TGE_SLOUPCE[1:]:
            df[c] = pd.to_numeric(df[c].astype(str).str.replace("\xa0", "").str.replace(" ", "").str.replace(",", "."), errors="coerce")
        if len(df) >= 100:                                # 24 H + 96 Q instrumentu; min prah proti torzu
            df.insert(0, "business_date", vcera)
            df["snapshot_ts"] = SNAP
            df.to_parquet(soubor, index=False)
            print(f"tge_rdb {vcera}: {len(df)} instrumentu")
        else:
            print(f"tge_rdb {vcera}: jen {len(df)} radku, neukladam (den mozna neni uzavren)")
except Exception as e:
    print(f"tge_rdb {vcera}: CHYBA {type(e).__name__}: {e} — pokracuji")

# ---------- ranni cteni trhu: TGE kontinual v 5:15 a 6:15 lokalne ----------
# Validace Ranniho tuseni: co trh "vi" rano o dnesnich dodavkach (kumulativni CT statistiky
# rozjednanych instrumentu). Pozitivni nalez trzni znalost validuje; nulovy ji NEVYVRACI,
# protoze ridke obchody neznamenaji prazdnou knihu.
try:
    from zoneinfo import ZoneInfo
    lok = NOW.astimezone(ZoneInfo("Europe/Warsaw"))
    if lok.hour in (5, 6) and 5 <= lok.minute <= 40:
        from bs4 import BeautifulSoup
        dnes = lok.strftime("%Y-%m-%d")
        r = requests.get("https://tge.pl/energia-elektryczna-rdb", headers=UA, timeout=120)
        tab = BeautifulSoup(r.text, "lxml").find_all("table")[0]
        radky = [[c.get_text(strip=True) for c in tr.find_all("td")] for tr in tab.find_all("tr")]
        radky = [x for x in radky if len(x) == 29 and "_" in x[0]]
        df = pd.DataFrame(radky, columns=TGE_SLOUPCE)
        for c in TGE_SLOUPCE[1:]:
            df[c] = pd.to_numeric(df[c].astype(str).str.replace("\xa0", "").str.replace(" ", "").str.replace(",", "."), errors="coerce")
        if len(df) >= 50 and str(df["instrument"].iloc[0]).startswith(dnes):
            df.insert(0, "business_date", dnes)
            df["snapshot_ts"] = SNAP
            soubor = f"data/tge_rano_{dnes}.parquet"
            if os.path.exists(soubor):
                df = pd.concat([pd.read_parquet(soubor), df], ignore_index=True)
            df.to_parquet(soubor, index=False)
            print(f"tge_rano {dnes}: snapshot {lok.strftime('%H:%M')} ({len(df)} instrumentu)")
except Exception as e:
    print(f"tge_rano: CHYBA {type(e).__name__}: {e} — pokracuji")

try:
    soubor = f"data/np_id_{vcera}.parquet"
    if not os.path.exists(soubor):
        j = requests.get(f"https://dataportal-api.nordpoolgroup.com/api/IntradayMarketStatistics?date={vcera}&deliveryArea=PL", headers=UA, timeout=60).json()
        df = pd.DataFrame(j.get("contracts", []))
        if len(df) >= 100:                                # 96 QH + 25 H kontraktu
            df.insert(0, "business_date", vcera)
            df["updateTime"] = j.get("updateTime")
            df["snapshot_ts"] = SNAP
            df.to_parquet(soubor, index=False)
            print(f"np_id {vcera}: {len(df)} kontraktu")
except Exception as e:
    print(f"np_id {vcera}: CHYBA {type(e).__name__}: {e} — pokracuji")

# Svodka se od 8.7.2026 generuje v privatnim repu PSE-svodka (Telegram); zde uz nebezi.
