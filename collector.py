# Sbira PSE snapshoty s casovou znackou. Spousti GitHub Actions kazdych 30 min.
# Snima CELY BPKD plan (vsech 37 sloupcu) na 3 dny dopredu -> zachyti revize.
import requests, os
from datetime import datetime, timezone, timedelta
import pandas as pd

BASE = "https://api.raporty.pse.pl/api/"
NOW  = datetime.now(timezone.utc)
SNAP = NOW.strftime("%Y-%m-%d %H:%M:%S")
DNES = NOW.strftime("%Y-%m-%d")

# nazev -> (mozne endpointy /fallback/, popis)
ENDPOINTY = {
    "BPKD":       ["pdgobpkd", "pdgob"],   # CELY plan koordynacyjny (37 sloupcu) -- HLAVNI cil
    "price_fcst": ["price-fcst"],          # prognoza CEN + odchylky (maly bonus)
}
DNI_DOPREDU = 3                            # dnes + zitra + pozitri (forecast horizont)

def stahni(endpoint, den):
    out, url = [], f"{BASE}{endpoint}?$filter=business_date eq '{den}'"
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
    endpoint = next((ep for ep in varianty if stahni(ep, DNES)), None)   # najdi funkcni
    if endpoint is None:
        print(f"{nazev}: zadny z {varianty} nevraci data"); continue
    radky = []
    for den in dny:
        radky += stahni(endpoint, den)
    if not radky:
        print(f"{nazev}: 0 radku"); continue
    df = pd.DataFrame(radky)
    df["snapshot_ts"] = SNAP            # KLIC: kdy jsme to videli (osa verzovani)
    soubor = f"data/{nazev}_{DNES}.parquet"
    if os.path.exists(soubor):
        df = pd.concat([pd.read_parquet(soubor), df], ignore_index=True)
    df.to_parquet(soubor, index=False)
    print(f"{nazev} ({endpoint}): +{len(radky)} radku, snapshot {SNAP}, celkem {len(df)}")
