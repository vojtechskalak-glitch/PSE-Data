# Sbira PSE snapshoty s casovou znackou. Spousti GitHub Actions kazdych 30 min.
# Snima CELY BPKD plan na 3 dny dopredu. KAZDY OBCHODNI DEN -> SVUJ SOUBOR (vsechny revize).
import requests, os
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
