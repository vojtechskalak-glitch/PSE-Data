# Sbira PSE snapshoty s casovou znackou. Spousti GitHub Actions kazdych 30 min (+ externi trigger).
# Snima CELY BPKD plan na 3 dny dopredu. KAZDY OBCHODNI DEN -> SVUJ SOUBOR (vsechny revize).
# poeb-rbn (bid stack energie): verzovani po periodach — perioda se ulozi poprve a pak jen kdyz se zmeni.
import requests, os, hashlib
from datetime import datetime, timezone, timedelta
import pandas as pd

BASE = "https://api.raporty.pse.pl/api/"
NOW  = datetime.now(timezone.utc)
SNAP = NOW.strftime("%Y-%m-%d %H:%M:%S")

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
dny = [(NOW + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DNI_DOPREDU)]

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

vcera = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
for den in [dny[0], vcera]:   # dnes + vcera (pulnocni dobihani poslednich period a pozdni revize)
    radky = stahni("poeb-rbn", den)
    if not radky:
        print(f"poeb_rbn {den}: nic nepublikovano"); continue
    novy = pd.DataFrame(radky)
    soubor = f"data/poeb_rbn_{den}.parquet"

    stary, stare_otisky = None, {}
    if os.path.exists(soubor):
        stary = pd.read_parquet(soubor)
        for per, g in stary.groupby("period"):            # posledni ulozena verze kazde periody
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
