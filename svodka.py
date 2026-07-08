# Ranni svodka: generator pravdepodobnostniho vyhledu cen odchylky (CEN) na den dodavky.
#
# Dva rezimy (vola je collector.py v casovych oknech, nebo rucne z CLI):
#   odpoledni  - po publikaci day-ahead aukce (~13:45 D-1) vygeneruje svodku na ZITREK
#                z DA krivky, tabulek modelu Vahy pasem (svodka_model.json) a JAO limitu.
#   ranni      - mezi 9:30 a 10:30 dopolni do dnesni svodky intradenni sekci
#                (posledni post-periodovy otisk ceny + objem knihy pod -510 PLN).
#
# PRAVIDLO LEAKAGE: generator NIKDY nepouziva finalni ceny (CEN/SK) dne, na ktery se
# svodka pise. Jen day-ahead, JAO, historicke tabulky, print a knihu do casu generovani.
#
# Vystup: svodka/RRRR-MM-DD.md (datum dne DODAVKY). Model: svodka_model.json (jen
# agregovane cetnosti z historie 14.3.-30.6.2026, zadna surova data, zadna strategie).
import json, os, sys
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

KOREN = os.path.dirname(os.path.abspath(__file__))
MODEL_SOUBOR = os.path.join(KOREN, "svodka_model.json")
SVODKA_DIR = os.path.join(KOREN, "svodka")
KURZ_EUR = 4.25
TZ = ZoneInfo("Europe/Warsaw")

# hranice pasem: konvence (lo, hi] — hodnota presne na hrane patri do nizsiho pasma
DA_HRANY = [-170, 0, 200, 500, 800, 1275]
DA_NAZVY = ["do −170", "−170 až 0", "0 až 200", "200 až 500",
            "500 až 800", "800 až 1275", "nad 1275"]
PREB_HRANY = [-510, -170, 0]
PREB_NAZVY = ["pod −510", "−510 až −170", "−170 až 0", "nad 0"]
DEF_HRANY = [500, 1275]
DEF_NAZVY = ["do 500", "500 až 1275", "nad 1275"]
VYHLAZENI_K = 8            # sila prilnuti ridkych bunek k marginalu DA-bucketu
SCHODISTE_HRANY = [800, 1000, 1275]           # DA pasma vecerniho deficitniho schodiste
SCHODISTE_P = [0.006, 0.053, 0.233, 1.0]      # P(CEN > 1275 | deficit) per pasmo

MARKER_RANNI = "*Intradenní aktualizace se doplní ráno mezi 9:30 a 10:30.*"
NADPIS_RANNI = "### Intradenní aktualizace"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
PSE = "https://api.raporty.pse.pl/api/"


# ---------------------------------------------------------------- pomocne
def pasmo(x, hrany):
    """Index pasma pro hodnotu x pri konvenci (lo, hi]."""
    return bisect_left(hrany, x)


def cz(x, des=0):
    """Ceske formatovani cisla: desetinna carka, mezera v tisicich, typograficke minus."""
    s = f"{x:,.{des}f}".replace(",", " ").replace(".", ",").replace("-", "−")
    return s


def pct(p):
    """Procento: pod 10 % s jednim desetinnym mistem, jinak cele."""
    v = 100 * p
    return cz(v, 1) + " %" if v < 10 else cz(v, 0) + " %"


def stahni_pse(endpoint, den):
    out, url = [], f"{PSE}{endpoint}?$filter=business_date eq '{den}'&$first=20000"
    while url:
        r = requests.get(url, timeout=60)
        if not r.ok:
            break
        d = r.json()
        out.extend(d.get("value", []))
        url = d.get("nextLink")
    return out


# ---------------------------------------------------------------- stavba modelu
def postav_model():
    """Z historickych finalu (data_raw, 14.3.-30.6.) spocita tabulky Vah pasem
    a prah objemu knihy; ulozi svodka_model.json. Bezi jen lokalne (data_raw neni v gitu)."""
    import pandas as pd
    fin = os.path.join(KOREN, "data_raw", "FINAL")

    def nacti(vzor, sloupce):
        casti = [pd.read_csv(os.path.join(fin, f))[["business_date", "period"] + sloupce]
                 for f in os.listdir(fin) if f.startswith(vzor)]
        return pd.concat(casti, ignore_index=True)

    cen = nacti("PSE_CENA_CEN_finalni_", ["cen_cost"])
    sk = nacti("PSE_ODCH_SK_", ["sk_cost"])
    da = nacti("PSE_CENA_SDAC_DA_", ["csdac_pln"])
    df = cen.merge(sk, on=["business_date", "period"]).merge(da, on=["business_date", "period"])
    df = df[(df["business_date"] >= "2026-03-14") & (df["business_date"] <= "2026-06-30")]
    df = df.dropna(subset=["cen_cost", "sk_cost", "csdac_pln"])
    df["hodina"] = df["period"].str[:2].astype(int)
    df["deficit"] = df["sk_cost"] < 0            # sk < 0 = deficit soustavy
    df["da_b"] = df["csdac_pln"].map(lambda x: pasmo(x, DA_HRANY))
    df["p_preb"] = df["cen_cost"].map(lambda x: pasmo(x, PREB_HRANY))
    df["p_def"] = df["cen_cost"].map(lambda x: pasmo(x, DEF_HRANY))

    def rozdeleni(sub, sloupec, n_pasem):
        cet = sub[sloupec].value_counts()
        n = len(sub)
        return [float(cet.get(i, 0)) / n if n else 0.0 for i in range(n_pasem)]

    # globalni a bucketove marginaly (fallback pro ridke bunky)
    glob = {
        "n": len(df),
        "p_deficit": float(df["deficit"].mean()),
        "prebytek": rozdeleni(df[~df["deficit"]], "p_preb", 4),
        "deficit": rozdeleni(df[df["deficit"]], "p_def", 3),
    }
    marg = {}
    for b in range(len(DA_NAZVY)):
        sub = df[df["da_b"] == b]
        if len(sub) == 0:
            marg[b] = dict(glob)
            marg[b]["n"] = 0
            continue
        preb, defi = sub[~sub["deficit"]], sub[sub["deficit"]]
        marg[b] = {
            "n": len(sub),
            "p_deficit": float(sub["deficit"].mean()),
            "prebytek": rozdeleni(preb, "p_preb", 4) if len(preb) else glob["prebytek"],
            "deficit": rozdeleni(defi, "p_def", 3) if len(defi) else glob["deficit"],
        }

    def vyhlad(cet, n, prior):
        """Bayesovske vyhlazeni: bunka + k pozorovani z prioru (marginalu bucketu)."""
        k = VYHLAZENI_K
        return [(cet[i] + k * prior[i]) / (n + k) for i in range(len(prior))]

    bunky = {}
    for b in range(len(DA_NAZVY)):
        for h in range(24):
            sub = df[(df["da_b"] == b) & (df["hodina"] == h)]
            preb, defi = sub[~sub["deficit"]], sub[sub["deficit"]]
            m = marg[b]
            cet_preb = [float((preb["p_preb"] == i).sum()) for i in range(4)]
            cet_def = [float((defi["p_def"] == i).sum()) for i in range(3)]
            bunky[f"{b}|{h}"] = {
                "n": len(sub),
                "p_deficit": (len(defi) + VYHLAZENI_K * m["p_deficit"]) / (len(sub) + VYHLAZENI_K),
                "prebytek": vyhlad(cet_preb, len(preb), m["prebytek"]),
                "deficit": vyhlad(cet_def, len(defi), m["deficit"]),
            }

    # prah objemu knihy pod -510 PLN: horni decil per-periodovych souctu z historie
    # (poledni okno h9-16, 100 dni; CSV v FINAL je torzo, plna historie je v parquetu)
    stack = pd.read_parquet(
        os.path.join(KOREN, "data_raw", "PSE_data", "PSE_BID_poeb_rbn_2026-03-14_az_2026-06-21.parquet"),
        columns=["business_date", "period", "ofp", "ofcd"])
    hluboke = stack[stack["ofcd"] < -510].groupby(["business_date", "period"])["ofp"].sum()
    vsechny = stack.groupby(["business_date", "period"]).size().index
    vol510 = hluboke.reindex(vsechny, fill_value=0.0)

    model = {
        "vytvoreno": datetime.now(TZ).strftime("%Y-%m-%d"),
        "vzorek": {"od": "2026-03-14", "do": "2026-06-30",
                   "dni": int(df["business_date"].nunique()), "ctvrthodin": int(len(df))},
        "kurz_eur": KURZ_EUR,
        "da_hrany": DA_HRANY, "da_nazvy": DA_NAZVY,
        "preb_hrany": PREB_HRANY, "preb_nazvy": PREB_NAZVY,
        "def_hrany": DEF_HRANY, "def_nazvy": DEF_NAZVY,
        "vyhlazeni_k": VYHLAZENI_K,
        "global": glob,
        "bucket_marginaly": {str(k): v for k, v in marg.items()},
        "bunky": bunky,
        "vol510": {"horni_decil_mw": round(float(vol510.quantile(0.9)), 1),
                   "median_mw": round(float(vol510.median()), 1),
                   "n_period": int(len(vol510))},
        "schodiste": {"da_hrany": SCHODISTE_HRANY, "p_draha": SCHODISTE_P},
    }
    with open(MODEL_SOUBOR, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=1)
    print(f"svodka_model.json: {model['vzorek']['dni']} dni, {model['vzorek']['ctvrthodin']} ctvrthodin, "
          f"P(deficit)={glob['p_deficit']:.3f}, vol510 decil={model['vol510']['horni_decil_mw']} MW")
    return model


def nacti_model():
    with open(MODEL_SOUBOR, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- jadro predikce
def pasma_ctvrthodiny(model, da_pln, hodina):
    """Pravdepodobnosti pasem CEN pro jednu ctvrthodinu: tabulka (DA-bucket x hodina)
    + deterministicka maska ze vzorce ceny (cepice pri prebytku, podlaha pri deficitu)."""
    b = pasmo(da_pln, DA_HRANY)
    c = model["bunky"][f"{b}|{hodina}"]
    p_def = c["p_deficit"]

    preb = list(c["prebytek"])
    for i in range(1, 4):                       # pasmo i ma dolni hranu PREB_HRANY[i-1]
        if PREB_HRANY[i - 1] >= da_pln:         # cele pasmo nad DA -> pri prebytku nemozne
            preb[i] = 0.0
    s = sum(preb) or 1.0
    preb = [x / s for x in preb]

    defi = list(c["deficit"])
    for i in range(0, 2):                       # pasmo i ma horni hranu DEF_HRANY[i]
        if DEF_HRANY[i] <= da_pln:              # cele pasmo pod DA -> pri deficitu nemozne
            defi[i] = 0.0
    s = sum(defi) or 1.0
    defi = [x / s for x in defi]

    return {"p_deficit": p_def, "prebytek": preb, "deficit": defi}


def hodinovy_vyhled(model, da_ctvrthodiny):
    """Agregace 4 ctvrthodin do hodinovych pravdepodobnosti klicovych pasem."""
    hodiny = []
    for h in range(24):
        qh = [x for x in da_ctvrthodiny if x["hodina"] == h]
        if not qh:
            continue
        acc = {"deficit": 0.0, "zapor": 0.0, "pod510": 0.0, "nad1275": 0.0}
        for q in qh:
            p = pasma_ctvrthodiny(model, q["da"], h)
            preb_w = 1 - p["p_deficit"]
            acc["deficit"] += p["p_deficit"]
            acc["zapor"] += preb_w * sum(p["prebytek"][0:3])   # CEN <= 0 (jen prebytkova vetev)
            acc["pod510"] += preb_w * p["prebytek"][0]
            acc["nad1275"] += p["p_deficit"] * p["deficit"][2]
        n = len(qh)
        hodiny.append({"hodina": h, "da": sum(q["da"] for q in qh) / n,
                       **{k: v / n for k, v in acc.items()}})
    return hodiny


def zaporne_epizody(hodiny):
    """Souvisle bezici hodiny se zapornym DA prumerem -> (start, delka) serazene sestupne."""
    ep, start = [], None
    for hd in hodiny + [{"hodina": 99, "da": 1}]:
        if hd["da"] < 0 and start is None:
            start = hd["hodina"]
        elif hd["da"] >= 0 and start is not None:
            ep.append((start, hd["hodina"] - start))
            start = None
    return sorted(ep, key=lambda x: -x[1])


def jao_zavora(den):
    """Pocet ctvrthodin dne se zavrenym dovozem (limitDown_PL = 0) z JAO. None = nedostupne."""
    try:
        od = datetime.strptime(den, "%Y-%m-%d").replace(tzinfo=TZ)
        do = od + timedelta(days=1)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        r = requests.get("https://publicationtool.jao.eu/core/api/data/allocationConstraint",
                         params={"FromUtc": od.astimezone(timezone.utc).strftime(fmt),
                                 "ToUtc": do.astimezone(timezone.utc).strftime(fmt)},
                         headers=UA, timeout=60)
        radky = r.json().get("data", [])
        if len(radky) < 90:
            return None
        nuly = [x for x in radky if x.get("limitDown_PL") == 0]
        hodiny_nul = sorted({datetime.fromisoformat(x["dateTimeUtc"].replace("Z", "+00:00"))
                             .astimezone(TZ).hour for x in nuly})
        return {"n": len(nuly), "z": len(radky), "hodiny": hodiny_nul}
    except Exception:
        return None


# ---------------------------------------------------------------- odpoledni rezim
def odpoledni_svodka(den, vynut=False):
    """Svodka na den dodavky `den` (RRRR-MM-DD). Idempotentni: soubor existuje -> nic."""
    os.makedirs(SVODKA_DIR, exist_ok=True)
    soubor = os.path.join(SVODKA_DIR, f"{den}.md")
    if os.path.exists(soubor) and not vynut:
        print(f"svodka {den}: uz existuje, preskakuji")
        return
    radky_da = stahni_pse("csdac-pln", den)
    if len(radky_da) < 90:
        print(f"svodka {den}: day-ahead jeste nepublikovan ({len(radky_da)} radku), preskakuji")
        return
    model = nacti_model()

    qh = [{"hodina": int(r["period"][:2]), "da": float(r["csdac_pln"])} for r in radky_da]
    hodiny = hodinovy_vyhled(model, qh)
    da_prumer = sum(h["da"] for h in hodiny) / len(hodiny)
    h_min = min(hodiny, key=lambda h: h["da"])
    h_max = max(hodiny, key=lambda h: h["da"])
    zap_hodiny = [h for h in hodiny if h["da"] < 0]
    epizody = zaporne_epizody(hodiny)
    jao = jao_zavora(den)
    ted = datetime.now(TZ)

    d = datetime.strptime(den, "%Y-%m-%d")
    den_tydne = ["pondělí", "úterý", "středu", "čtvrtek", "pátek", "sobotu", "neděli"][d.weekday()]

    t = []
    t.append(f"## {d.day}. {d.month}. {d.year} — svodka na {den_tydne}\n")
    t.append(f"Svodka shrnuje, co je o cenách odchylky na {den_tydne} známo den předem. "
             f"Vygenerována {ted.day}. {ted.month}. v {ted:%H:%M} z výsledků day-ahead aukce "
             f"(SDAC — celoevropská denní aukce elektřiny, ceny na každou čtvrthodinu zítřka "
             f"známé ~13:45), z historických tabulek a z limitů přeshraničního obchodu. "
             f"Ceny v PLN za MWh; dělení kurzem 4,25 dá přibližně eura.\n")

    # --- day-ahead krivka
    t.append("### Day-ahead křivka\n")
    veta = (f"Průměr day-ahead ceny je {cz(da_prumer)} PLN ({cz(da_prumer / KURZ_EUR)} EUR), "
            f"minimum {cz(h_min['da'])} PLN v hodině {h_min['hodina']}–{h_min['hodina'] + 1}, "
            f"maximum {cz(h_max['da'])} PLN ({cz(h_max['da'] / KURZ_EUR)} EUR) "
            f"v hodině {h_max['hodina']}–{h_max['hodina'] + 1}.")
    if zap_hodiny:
        veta += (f" Záporných hodin je {len(zap_hodiny)}: "
                 + ", ".join(f"{h['hodina']}–{h['hodina'] + 1}" for h in zap_hodiny) + ".")
    else:
        veta += " Žádná hodina nemá zápornou cenu."
    t.append(veta + "\n")

    # --- tabulka vah pasem
    t.append("### Pravděpodobnosti pásem ceny odchylky\n")
    t.append(f"CEN je cena, kterou polský provozovatel soustavy PSE každou čtvrthodinu vypořádá "
             f"pozici obchodníka dojetou do nerovnováhy. Tabulka níže je výstup modelu Váhy pásem: "
             f"pravděpodobnosti cenových pásem CEN podle day-ahead ceny a hodiny dne, spočtené "
             f"z {model['vzorek']['dni']} dní historie (14. 3. – 30. 6.). Přes tabulky jde tvrdá mez "
             f"ze vzorce ceny: při přebytku v soustavě CEN nikdy neskončí nad day-ahead cenou (čepice), "
             f"při deficitu nikdy pod ní (podlaha). Sloupec „deficit“ říká, jak často byla soustava "
             f"v tuto hodinu a při této cenové hladině krátká; zbylé sloupce jsou pravděpodobnosti "
             f"záporné CEN, hlubokého propadu pod −510 PLN (−120 EUR) a drahé špičky nad 1275 PLN "
             f"(300 EUR).\n")
    t.append("| Hodina | DA PLN | Deficit | CEN < 0 | CEN ≤ −510 | CEN > 1275 |")
    t.append("|---|---|---|---|---|---|")
    for h in hodiny:
        t.append(f"| {h['hodina']}–{h['hodina'] + 1} | {cz(h['da'])} | {pct(h['deficit'])} "
                 f"| {pct(h['zapor'])} | {pct(h['pod510'])} | {pct(h['nad1275'])} |")
    t.append("")
    top_p = max(hodiny, key=lambda h: h["pod510"])
    top_d = max(hodiny, key=lambda h: h["nad1275"])
    t.append(f"Nejvyšší riziko hlubokého propadu má hodina {top_p['hodina']}–{top_p['hodina'] + 1} "
             f"({pct(top_p['pod510'])}), nejvyšší riziko drahé špičky hodina "
             f"{top_d['hodina']}–{top_d['hodina'] + 1} ({pct(top_d['nad1275'])}).\n")

    # --- sestihodinovy utes
    t.append("### Šestihodinový útes\n")
    veta = ("Pravidlo polské podpory obnovitelných zdrojů: když je day-ahead cena záporná šest "
            "a více hodin v řadě, výrobci za tyto hodiny podporu nedostanou a mají důvod výrobu "
            "zastavit sami — nabídková strana se v takových hodinách chová jinak. ")
    utes = [e for e in epizody if e[1] >= 6]
    temer = [e for e in epizody if 3 <= e[1] <= 5]
    if utes:
        vycet = ", ".join(f"{s}–{s + n} ({n} h)" for s, n in utes)
        veta += f"**Dnes platí**: záporná řada {vycet}."
    elif temer:
        vycet = ", ".join(f"{s}–{s + n} ({n} h)" for s, n in temer)
        veta += f"Dnes neplatí, ale těsně: záporná epizoda {vycet} — do útesu chybí {6 - temer[0][1]} h."
    elif zap_hodiny:
        veta += "Dnes neplatí; záporné hodiny jsou, ale žádná řada nedosahuje ani tří hodin."
    else:
        veta += "Dnes neplatí — žádná záporná hodina."
    t.append(veta + "\n")

    # --- JAO zavora
    if jao is not None:
        t.append("### Alokační závora\n")
        veta = (f"Limity, kterými PSE den předem svazuje polskou obchodní pozici vůči zahraničí "
                f"(publikuje evropská aukční kancelář JAO). Dovoz elektřiny do Polska je úplně "
                f"zavřený v {jao['n']} čtvrthodinách z {jao['z']}")
        if jao["hodiny"]:
            veta += f", v hodinách {jao['hodiny'][0]}–{jao['hodiny'][-1] + 1}"
        veta += ". Zavřený dovoz znamená, že přebytek se nedá ředit exportem sousedů opačným směrem."
        t.append(veta + "\n")
    else:
        t.append("### Alokační závora\n")
        t.append("Data JAO se nepodařilo stáhnout; sekce dnes chybí.\n")

    # --- vecerni deficitni schodiste
    t.append("### Večerní deficitní schodiště\n")
    t.append("Když je soustava večer krátká, pravděpodobnost čtvrthodiny nad 1275 PLN roste "
             "s day-ahead cenou schodovitě: 0,6 % pod 800 PLN, 5,3 % mezi 800 a 1000, "
             "23,3 % mezi 1000 a 1275 — a nad 1275 PLN už drahá čtvrthodina není riziko, "
             "ale mechanika (podlaha z day-ahead).\n")
    vecer = [h for h in hodiny if 17 <= h["hodina"] <= 23]
    stupne = [(h, pasmo(h["da"], SCHODISTE_HRANY)) for h in vecer]
    nej = max(stupne, key=lambda x: x[1])
    if nej[1] == 0:
        t.append(f"Večer (17–24 h) zůstává celý na nejnižším stupni: žádná hodina nemá "
                 f"day-ahead nad 800 PLN (maximum {cz(max(h['da'] for h in vecer))} PLN).\n")
    else:
        vypis = ", ".join(f"{h['hodina']}–{h['hodina'] + 1} ({cz(h['da'])} PLN → {pct(SCHODISTE_P[s])})"
                          for h, s in stupne if s > 0)
        t.append(f"Večerní hodiny nad prvním stupněm: {vypis}.\n")

    t.append(MARKER_RANNI + "\n")
    with open(soubor, "w", encoding="utf-8") as f:
        f.write("\n".join(t))
    print(f"svodka {den}: vygenerovana ({soubor})")


# ---------------------------------------------------------------- ranni rezim
def ranni_aktualizace(den, vynut=False):
    """Dopolni do svodky dne `den` intradenni sekci. Idempotentni: sekce existuje -> nic."""
    import pandas as pd
    soubor = os.path.join(SVODKA_DIR, f"{den}.md")
    if not os.path.exists(soubor):
        print(f"svodka {den}: soubor neexistuje, ranni aktualizace nema kam")
        return
    with open(soubor, encoding="utf-8") as f:
        obsah = f.read()
    if NADPIS_RANNI in obsah and not vynut:
        print(f"svodka {den}: intradenni sekce uz existuje, preskakuji")
        return
    model = nacti_model()
    ted = datetime.now(TZ)
    t = [f"{NADPIS_RANNI} ({ted:%H:%M})\n"]

    # posledni post-periodovy otisk ceny z price-fcst (print ~15-20 min po konci periody)
    try:
        pf = pd.read_parquet(os.path.join(KOREN, "data", f"price_fcst_{den}.parquet"))
        posl = pf[pf["snapshot_ts"] == pf["snapshot_ts"].max()].copy()
        snap_utc = datetime.strptime(str(posl["snapshot_ts"].iloc[0]), "%Y-%m-%d %H:%M:%S")
        snap_lok = snap_utc.replace(tzinfo=timezone.utc).astimezone(TZ)
        mez = (snap_lok - timedelta(minutes=20)).replace(tzinfo=None)   # print je spolehlivy az ~20 min zpetne
        posl["konec"] = pd.to_datetime(posl["dtime"])
        realizovane = posl[(posl["konec"] <= mez) & posl["cen_fcst"].notna()].sort_values("konec")
        if len(realizovane):
            r = realizovane.iloc[-1]
            cena = float(r["cen_fcst"])
            vsechna_pasma = PREB_NAZVY[:3] + ["0 až 500", "500 až 1275", "nad 1275"]
            p_idx = pasmo(cena, PREB_HRANY[:2] + [0, 500, 1275])
            t.append(f"Poslední otisk ceny — odhad CEN, který PSE publikuje ~15–20 minut po konci "
                     f"každé čtvrthodiny: perioda {r['period']} skončila na {cz(cena)} PLN "
                     f"({cz(cena / KURZ_EUR)} EUR), tedy v pásmu {vsechna_pasma[p_idx]}. "
                     f"Otisk čten ze snímku {snap_lok:%H:%M}.\n")
        else:
            t.append("Poslední otisk ceny zatím není k dispozici (snímek příliš čerstvý).\n")
    except Exception as e:
        t.append("Otisk ceny se nepodařilo přečíst (data chybí).\n")
        print(f"svodka {den}: print CHYBA {type(e).__name__}: {e}")

    # objem knihy pod -510 PLN vs historicky horni decil
    try:
        kn = pd.read_parquet(os.path.join(KOREN, "data", f"poeb_rbn_{den}.parquet"))
        posledni_per = kn.loc[kn["dtime"].idxmax(), "period"]
        g = kn[kn["period"] == posledni_per]
        g = g[g["snapshot_ts"] == g["snapshot_ts"].max()]
        vol = float(g.loc[g["ofcd"] < -510, "ofp"].sum())
        prah = model["vol510"]["horni_decil_mw"]
        vztah = "NAD historickým horním decilem" if vol > prah else "pod historickým horním decilem"
        t.append(f"Kniha nabídek bilanční energie (nabídky, ze kterých PSE vybírá při vyrovnávání "
                 f"soustavy) má v poslední publikované periodě ({posledni_per}) pod −510 PLN "
                 f"{cz(vol)} MW — {vztah} ({cz(prah)} MW, medián {cz(model['vol510']['median_mw'])} MW). "
                 f"Vysoký objem hluboko v knize historicky doprovází dny s propady CEN.\n")
    except Exception as e:
        t.append("Objem knihy pod −510 PLN se nepodařilo přečíst (data chybí).\n")
        print(f"svodka {den}: kniha CHYBA {type(e).__name__}: {e}")

    nova_sekce = "\n".join(t)
    if MARKER_RANNI in obsah:
        obsah = obsah.replace(MARKER_RANNI, nova_sekce)
    elif NADPIS_RANNI in obsah and vynut:
        obsah = obsah[:obsah.index(NADPIS_RANNI)] + nova_sekce
    else:
        obsah = obsah.rstrip() + "\n\n" + nova_sekce
    with open(soubor, "w", encoding="utf-8") as f:
        f.write(obsah)
    print(f"svodka {den}: intradenni sekce doplnena")


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Ranni svodka: model a generator")
    ap.add_argument("rezim", choices=["postav-model", "odpoledni", "ranni"])
    ap.add_argument("--den", help="den dodavky RRRR-MM-DD (default: zitra pro odpoledni, dnes pro ranni)")
    ap.add_argument("--vynut", action="store_true", help="prepis existujici vystup")
    a = ap.parse_args()
    if a.rezim == "postav-model":
        postav_model()
    elif a.rezim == "odpoledni":
        den = a.den or (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        odpoledni_svodka(den, vynut=a.vynut)
    else:
        den = a.den or datetime.now(TZ).strftime("%Y-%m-%d")
        ranni_aktualizace(den, vynut=a.vynut)
