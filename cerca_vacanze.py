import streamlit as st
import pandas as pd
import io
import math
import requests
from datetime import date, timedelta, datetime
import plotly.express as px

# Configurazione della pagina
st.set_page_config(page_title="Cerca Vacanze", layout="wide")

# ============================================================================
# RICERCA GEOGRAFICA REALE
# Tre servizi esterni combinati:
# 1. Nominatim (OpenStreetMap, gratuito) → geocodifica la città di partenza
# 2. SerpApi → Google Hotels (a pagamento oltre le 250 ricerche/mese gratuite)
#    per SCOPRIRE gli hotel disponibili in una zona, con i loro prezzi reali
#    (già comprensivi delle commissioni di Booking/Expedia/ecc.)
# 3. OSRM (motore di instradamento open-source, server demo pubblico gratuito)
#    per calcolare km ed ore di guida REALI dall'origine a ciascun hotel
# ============================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def geocodifica_citta(nome_citta):
    """Nominatim (OpenStreetMap): nome città -> (lat, lon). Gratuito, nessuna chiave.
    Cache di 1h: usata sia per la città di partenza sia per l'eventuale punto di riferimento
    manuale (es. 'Spiaggia di Riccione'), che viene ricalcolato ad ogni rerun essendo un filtro
    locale istantaneo — senza cache martellerebbe Nominatim ad ogni interazione con la sidebar."""
    _incrementa_contatore("nominatim")
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": nome_citta, "format": "json", "limit": 1},
            headers={"User-Agent": "SmartHolidayFinder/1.0 (uso personale)"},
            timeout=10,
        )
        r.raise_for_status()
        risultati = r.json()
        if not risultati:
            return None
        return float(risultati[0]["lat"]), float(risultati[0]["lon"])
    except Exception:
        return None


def _haversine_km(lat1, lon1, lat2, lon2):
    """Distanza in linea d'aria (km) tra due coordinate."""
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _incrementa_contatore(servizio):
    """Incrementa il contatore di chiamate di rete REALMENTE eseguite verso 'servizio' in questa
    sessione. Va chiamato dentro al corpo delle funzioni @st.cache_data (mai nei loro wrapper):
    così facendo scatta solo quando la funzione viene davvero eseguita (cache MISS), non quando
    la risposta arriva dalla cache — che infatti non genera alcuna nuova chiamata di rete."""
    contatori = st.session_state.setdefault("contatore_chiamate", {})
    contatori[servizio] = contatori.get(servizio, 0) + 1


OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",       # istanza principale
    "https://overpass.kumi.systems/api/interpreter",  # mirror di riserva
]


@st.cache_data(ttl=2592000, show_spinner=False)  # 30 giorni: le coste/laghi non si spostano
def punto_acqua_piu_vicino(lat, lon, tipo_acqua, raggio_ricerca_m=20000):
    """Overpass API (OpenStreetMap, gratuito): trova le coordinate del tratto di costa marina o
    di riva di lago più vicino alle coordinate date, entro un raggio di ricerca.
    tipo_acqua: 'mare' o 'lago'. Ritorna (lat, lon) del punto più vicino trovato (approssimato al
    vertice più vicino della geometria, non al punto esatto sul segmento), o None se non trovato
    nel raggio (esito stabile, quindi legittimamente cache-abile a lungo).
    IMPORTANTE: in caso di errore di rete (timeout, HTTP 4xx/5xx, ecc.) la funzione ora SOLLEVA
    l'eccezione invece di restituire None: se la 'silenziassimo' qui dentro, @st.cache_data la
    cache-erebbe come risultato valido per 30 giorni, congelando un problema temporaneo (es. un
    rate-limit momentaneo di Overpass) come se fosse un fallimento permanente. Chi chiama questa
    funzione (distanza_acqua_auto, NON cache-ata) si occupa di intercettare l'eccezione e farla
    ritentare al prossimo utilizzo.
    Prova in sequenza più mirror pubblici (OVERPASS_MIRRORS): se il primo rifiuta la richiesta o
    non risponde, tenta il successivo prima di arrendersi. Invia sempre uno User-Agent
    identificativo: senza (come nella versione precedente) alcuni mirror rispondono 406/chiudono
    la connessione, scambiando la richiesta per traffico da bot anonimo."""
    _incrementa_contatore("overpass")
    filtro = '["natural"="coastline"]' if tipo_acqua == "mare" else '["natural"="water"]["water"~"lake|reservoir"]'
    query = f"""
    [out:json][timeout:15];
    (
      way{filtro}(around:{raggio_ricerca_m},{lat},{lon});
    );
    out geom;
    """
    headers = {"User-Agent": "SmartHolidayFinder/1.0 (uso personale)"}
    ultimo_errore = None
    for url in OVERPASS_MIRRORS:
        try:
            r = requests.post(url, data={"data": query}, headers=headers, timeout=20)
            r.raise_for_status()
            elementi = r.json().get("elements", [])
            break  # richiesta riuscita: esco dal ciclo mirror, elementi è pronto per l'uso sotto
        except Exception as e:
            ultimo_errore = e
            elementi = None
            continue
    else:
        raise ultimo_errore  # tutti i mirror hanno fallito: propago l'ultimo errore incontrato

    if not elementi:
        return None
    distanza_min, punto_min = None, None
    for el in elementi:
        for nodo in el.get("geometry") or []:
            if nodo is None:
                continue
            d = _haversine_km(lat, lon, nodo["lat"], nodo["lon"])
            if distanza_min is None or d < distanza_min:
                distanza_min, punto_min = d, (nodo["lat"], nodo["lon"])
    return punto_min


def distanza_acqua_auto(lat, lon, tipo_acqua):
    """Combina Overpass (trova il punto di costa/lago più vicino) e OSRM (calcola km/tempo di
    guida REALI fino a quel punto, con fallback automatico a stima lineare se OSRM non risponde,
    già gestito da calcola_distanze_reali). Ritorna un dict {"km", "ore", "metodo"} oppure None
    se non è stato trovato alcun tratto di costa/lago nel raggio di ricerca, o se Overpass ha
    fallito per un errore di rete.
    NON è cache-ata a questo livello (a differenza delle funzioni che chiama): un errore di rete
    va ritentato al prossimo rerun, non congelato in cache. Il messaggio di errore reale (non un
    generico 'fallito') viene salvato in session_state per poterlo mostrare nella diagnostica.
    ATTENZIONE: due chiamate di rete per ogni hotel (Overpass + OSRM), seppur cache-ate quando
    riescono; per questo va posizionato come ULTIMO filtro nella catena (si valuta solo sugli
    hotel che hanno già superato gli altri filtri, più economici)."""
    try:
        punto = punto_acqua_piu_vicino(lat, lon, tipo_acqua)
    except Exception as e:
        errori = st.session_state.setdefault("errori_overpass", [])
        messaggio = f"{type(e).__name__}: {e}"
        if messaggio not in errori:
            errori.append(messaggio)
        return None
    if punto is None:
        return None
    risultati = calcola_distanze_reali(lat, lon, [punto])
    return risultati[0] if risultati else None


@st.cache_data(ttl=3600, show_spinner=False)
def distanze_auto_da_punto(punto_lat, punto_lon, coordinate_hotel):
    """coordinate_hotel: tupla di (lat, lon). Calcola km/tempo di guida REALI dal punto di
    riferimento manuale a TUTTI gli hotel in un'UNICA chiamata OSRM batch (invece di una per
    hotel), con lo stesso fallback lineare automatico di calcola_distanze_reali."""
    return calcola_distanze_reali(punto_lat, punto_lon, list(coordinate_hotel))


def calcola_distanze_reali(origine_lat, origine_lon, destinazioni):
    """
    destinazioni: lista di (lat, lon).
    Prova OSRM (routing reale, un'unica chiamata batch via 'table service').
    Ritorna una lista di dict: {"km": float|None, "ore": float|None, "metodo": str}.
    Tre casi possibili per ogni destinazione:
    - OSRM trova un percorso stradale reale -> km/ore reali, metodo "OSRM (instradamento reale)".
    - OSRM risponde correttamente ma segnala ESPLICITAMENTE che non esiste alcun percorso stradale
      (duration/distance nulli nella risposta) -> è il caso tipico di un'isola raggiungibile solo
      via traghetto/aereo: km/ore vengono impostati a None (NON a una stima lineare, che sarebbe
      fuorviante perché presuppone comunque una strada) e metodo "Non raggiungibile in auto".
    - OSRM stesso non risponde (errore di rete/timeout, non un giudizio sul percorso) -> unico caso
      in cui si ricade su una stima in linea d'aria corretta con un fattore di curvatura stradale,
      perché qui non sappiamo ancora se la meta sia raggiungibile o meno.
    """
    if not destinazioni:
        return []
    _incrementa_contatore("osrm")

    try:
        coords = f"{origine_lon},{origine_lat};" + ";".join(f"{lon},{lat}" for lat, lon in destinazioni)
        r = requests.get(
            f"https://router.project-osrm.org/table/v1/driving/{coords}",
            params={"annotations": "duration,distance", "sources": "0"},
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") == "Ok":
            durations = data["durations"][0][1:]
            distances = data["distances"][0][1:]
            risultati = []
            for d, dist in zip(durations, distances):
                if d is None or dist is None:
                    # OSRM ha risposto ma non ha trovato NESSUN percorso stradale per questa
                    # destinazione specifica: molto probabilmente un'isola o una meta separata
                    # dalla terraferma, non un errore temporaneo. Niente stima lineare qui.
                    risultati.append({"km": None, "ore": None, "metodo": "Non raggiungibile in auto"})
                else:
                    risultati.append({"km": dist / 1000, "ore": d / 3600, "metodo": "OSRM (instradamento reale)"})
            return risultati
    except Exception:
        pass

    # Fallback completo: OSRM stesso non ha risposto (rete/timeout) — qui non sappiamo se la meta
    # sia raggiungibile in auto o meno, quindi usiamo una stima in linea d'aria come approssimazione
    # provvisoria, da ritentare al prossimo rerun (questa funzione non è cache-ata).
    risultati = []
    for lat, lon in destinazioni:
        km_lin = _haversine_km(origine_lat, origine_lon, lat, lon)
        km_stimati = km_lin * 1.55  # fattore di curvatura stradale (percorsi alpini/collinari)
        risultati.append({"km": km_stimati, "ore": km_stimati / 65, "metodo": "Stima lineare (fallback)"})
    return risultati


# ID amenità ufficiali di Google Hotels (documentazione SerpApi) utilizzabili come filtro
# lato server nella richiesta stessa, invece che come ricerca testuale dopo il download.
AMENITA_ID_SERPAPI = {
    "piscina": 6,             # Pool (generico: interna o esterna)
    "wifi": 35,                # Free Wi-Fi
    "colazione": 9,            # Free breakfast
    "parcheggio": 1,           # Free parking
    "animali": 19,             # Pet-friendly
    "palestra": 7,             # Fitness center
    "ristorante": 8,           # Restaurant
    "spa": 10,                 # Spa
    "bar": 15,                 # Bar
    "servizio_camera": 22,     # Room service
    "aria_condizionata": 40,   # Air-conditioned
    "accesso_spiaggia": 11,    # Beach access
    "bambini": 12,             # Child-friendly
    "all_inclusive": 52,       # All-inclusive available
    "accessibile": 53,         # Wheelchair accessible
    "ricarica_ev": 61,         # EV charger
}
# La valutazione minima di Google Hotels è disponibile solo a 3 soglie fisse (non è un valore
# libero): 3.5+, 4.0+ e 4.5+, ciascuna con un codice numerico dedicato.
RATING_CODICI_SERPAPI = {3.5: 7, 4.0: 8, 4.5: 9}


def cerca_hotel_zona(zona_query, check_in_str, check_out_str, adulti, bambini, api_key, max_risultati=10, filtri_extra=None, eta_bambini=None):
    """SerpApi → Google Hotels: cerca hotel per zona/città. Ritorna (lista_proprieta, errore).
    filtri_extra: dict di parametri aggiuntivi (amenities, hotel_class, rating, free_cancellation)
    da includere direttamente nella richiesta, così Google Hotels restituisce solo hotel già
    pertinenti invece di scaricarne un campione da filtrare dopo.
    eta_bambini: lista di età (una per ogni bambino). Google Hotels la richiede OBBLIGATORIAMENTE
    quando 'children' > 0, altrimenti la richiesta viene rifiutata con errore 400."""
    try:
        params = {
            "engine": "google_hotels",
            "q": zona_query,
            "check_in_date": check_in_str,
            "check_out_date": check_out_str,
            "adults": int(adulti),
            "children": int(bambini),
            "currency": "CHF",
            "hl": "it",
            "gl": "ch",
            "api_key": api_key,
        }
        if int(bambini) > 0:
            eta_valide = (eta_bambini or [])[:int(bambini)]
            params["children_ages"] = ",".join(str(int(e)) for e in eta_valide)
        if filtri_extra:
            params.update(filtri_extra)
        r = requests.get("https://serpapi.com/search", params=params, timeout=25)
        # Leggiamo SEMPRE il corpo della risposta prima di controllare lo status: se SerpApi
        # risponde 400/4xx include comunque un campo "error" col motivo preciso, che andrebbe
        # perso se chiamassimo raise_for_status() prima (finirebbe in un generico "Bad Request").
        try:
            data = r.json()
        except ValueError:
            data = None
        if data is not None and "error" in data:
            return [], f"Errore API: {data['error']}"
        r.raise_for_status()
        return data.get("properties", [])[:max_risultati], None
    except requests.exceptions.RequestException as e:
        return [], f"Errore di connessione: {e}"
    except Exception as e:
        return [], f"Errore imprevisto: {e}"


@st.cache_data(ttl=120, show_spinner=False)
def controlla_quota_serpapi(api_key):
    """Interroga l'Account API di SerpApi (gratuita, non consuma ricerche del piano) per
    sapere quante ricerche sono già state usate questo mese e quante ne restano.
    Cache di 2 minuti per evitare chiamate ripetute ad ogni rerun di Streamlit.
    Ritorna (dati_quota, errore); dati_quota è un dict oppure None in caso di errore."""
    try:
        r = requests.get("https://serpapi.com/account.json", params={"api_key": api_key}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            return None, f"Errore API: {data['error']}"
        return {
            "usate_questo_mese": data.get("this_month_usage"),
            "limite_mensile": data.get("searches_per_month"),
            "rimanenti": data.get("plan_searches_left"),
            "crediti_extra": data.get("extra_credits", 0),
            "limite_orario": data.get("throughput"),
        }, None
    except requests.exceptions.RequestException as e:
        return None, f"Errore di connessione: {e}"
    except Exception as e:
        return None, f"Errore imprevisto: {e}"


# Parole chiave (IT/EN) cercate tra le amenità e la descrizione restituite da Google Hotels.
# Servono per due scopi: (1) mostrare le icone riassuntive in tabella anche per i filtri
# server-side, e (2) filtrare il database di esempio (che non passa mai da Google Hotels).
# Per le voci sci uso solo frasi specifiche (mai "sci" da solo: comparirebbe come falso
# positivo dentro parole come "asciugamano").
PAROLE_CHIAVE_SERVIZI = {
    "piscina": ["pool", "piscina"],
    "wifi": ["wifi", "wi-fi"],
    "colazione": ["breakfast", "colazione"],
    "parcheggio": ["parking", "parcheggio"],
    "animali": ["pet friendly", "pet-friendly", "pets allowed", "animali ammessi", "animali domestici"],
    "palestra": ["fitness center", "gym", "palestra"],
    "ristorante": ["restaurant", "ristorante"],
    "spa": ["spa", "centro benessere", "wellness"],
    "bar": ["bar", "lounge"],
    "servizio_camera": ["room service", "servizio in camera"],
    "aria_condizionata": ["air conditioning", "air-conditioned", "aria condizionata"],
    "accesso_spiaggia": ["beach access", "accesso spiaggia", "beachfront"],
    "bambini": ["child-friendly", "kid-friendly", "adatto ai bambini", "family friendly"],
    "all_inclusive": ["all-inclusive", "all inclusive"],
    "accessibile": ["wheelchair accessible", "accessibile in sedia a rotelle", "accessibile ai disabili"],
    "ricarica_ev": ["ev charger", "electric vehicle charging", "colonnina di ricarica", "ricarica auto elettrica"],
    "vicino_sci": ["ski-in", "ski-out", "ski lift", "ski storage", "impianti di risalita",
                   "piste da sci", "seggiovia", "funivia", "sci alpino", "vicino alle piste"],
    "deposito_sci": ["ski storage", "deposito sci", "noleggio sci", "ski rental"],
    "scuola_sci": ["ski school", "scuola sci", "scuola di sci", "kids ski"],
}



def _controlla_servizio(amenities, descrizione, chiave):
    """Cerca una qualsiasi delle parole chiave associate a `chiave` tra amenità e descrizione."""
    testo = " ".join(amenities or []).lower() + " " + (descrizione or "").lower()
    return any(parola in testo for parola in PAROLE_CHIAVE_SERVIZI[chiave])


def esegui_ricerca_geografica(origine_lat, origine_lon, zone, check_in, check_out, adulti, bambini, api_key, filtri_extra=None, max_risultati=10, eta_bambini=None, categoria_ricerca="Hotel"):
    """
    Orchestratore: per ogni zona interroga Google Hotels (SerpApi), poi calcola in un'unica
    chiamata OSRM per zona le distanze/durate reali di tutti gli hotel trovati in quella zona.
    categoria_ricerca: etichetta ("Hotel" o "B&B / Casa vacanza") assegnata a ogni risultato di
    questa chiamata, per poterli distinguere quando le due ricerche vengono unite nella stessa tabella.
    Ritorna (lista_hotel_trovati, lista_avvisi).
    """
    hotel_trovati = []
    avvisi = []

    for zona in zone:
        proprieta, errore = cerca_hotel_zona(
            zona, check_in.isoformat(), check_out.isoformat(), adulti, bambini, api_key,
            max_risultati=max_risultati, filtri_extra=filtri_extra, eta_bambini=eta_bambini,
        )
        if errore:
            avvisi.append(f"Zona '{zona}' ({categoria_ricerca}): {errore}")
            continue
        if not proprieta:
            avvisi.append(f"Zona '{zona}' ({categoria_ricerca}): nessun risultato trovato da Google Hotels")
            continue

        coordinate = []
        proprieta_valide = []
        for p in proprieta:
            gps = p.get("gps_coordinates") or {}
            lat, lon = gps.get("latitude"), gps.get("longitude")
            prezzo = (p.get("rate_per_night") or {}).get("extracted_lowest")
            if lat is None or lon is None or prezzo is None:
                continue
            coordinate.append((lat, lon))
            proprieta_valide.append(p)

        if not proprieta_valide:
            avvisi.append(f"Zona '{zona}' ({categoria_ricerca}): risultati trovati ma senza coordinate o prezzo utilizzabili")
            continue

        distanze = calcola_distanze_reali(origine_lat, origine_lon, coordinate)

        for p, dist in zip(proprieta_valide, distanze):
            gps = p["gps_coordinates"]
            amenities = p.get("amenities")
            descrizione = p.get("description", "")
            campi_servizio = {
                chiave: _controlla_servizio(amenities, descrizione, chiave)
                for chiave in PAROLE_CHIAVE_SERVIZI
            }
            hotel_trovati.append({
                "nome": p.get("name", "Struttura senza nome"),
                "regione": zona,
                "categoria_ricerca": categoria_ricerca,
                "lat": gps["latitude"],
                "lon": gps["longitude"],
                **campi_servizio,
                "cancellazione_gratuita": bool(p.get("free_cancellation", False)),
                "categoria_stelle": p.get("extracted_hotel_class"),
                "valutazione": p.get("overall_rating"),
                "prezzo_notte_chf": float(p["rate_per_night"]["extracted_lowest"]),
                "distanza_km": round(dist["km"], 1) if dist["km"] is not None else None,
                "ore_guida": round(dist["ore"], 1) if dist["ore"] is not None else None,
                "fonte_prezzo": "🟢 Google Hotels (tempo reale)",
                "fonte_distanza": dist["metodo"],
                "descrizione": descrizione,
                "tipo": p.get("type", "hotel"),
                "link": p.get("link", ""),
            })

    return hotel_trovati, avvisi


# --- FORMATTAZIONE SVIZZERA (apostrofo come separatore delle migliaia) ---
def swiss_num(x, decimals=0):
    """Formatta un numero con l'apostrofo come separatore delle migliaia (stile svizzero)."""
    s = f"{x:,.{decimals}f}"
    return s.replace(",", "'")

def swiss_chf(x, decimals=0):
    return f"CHF {swiss_num(x, decimals)}"


def icone_servizi(dest):
    """Riassume i servizi disponibili in un hotel come sequenza di icone, per la tabella."""
    mappa = [
        ("piscina", "🏊"), ("wifi", "📶"), ("colazione", "☕"), ("parcheggio", "🅿️"),
        ("animali", "🐾"), ("cancellazione_gratuita", "✅"),
        ("palestra", "🏋️"), ("ristorante", "🍽️"), ("spa", "💆"), ("bar", "🍸"),
        ("servizio_camera", "🛎️"), ("aria_condizionata", "❄️"), ("accesso_spiaggia", "🏖️"),
        ("bambini", "👶"), ("all_inclusive", "🍹"), ("accessibile", "♿"), ("ricarica_ev", "🔌"),
        ("vicino_sci", "⛷️"), ("deposito_sci", "🎿"), ("scuola_sci", "🏫"),
    ]
    icone = [icona for chiave, icona in mappa if dest.get(chiave)]
    return " ".join(icone) if icone else "—"


def testo_categoria(dest):
    stelle = dest.get("categoria_stelle")
    return "★" * int(stelle) if stelle else "n/d"


def testo_valutazione(dest):
    val = dest.get("valutazione")
    return f"{val:.1f} ⭐" if val is not None else "n/d"


# ============================================================================
# FORMATTAZIONE EXCEL (colonne leggibili, testo a capo, colori tenui omogenei)
# ============================================================================
# Ogni colonna: (chiave nel DataFrame, intestazione, tipo, larghezza minima, larghezza massima)
# tipo: "testo" (allineato a sinistra, va a capo), "centro" (valori brevi, centrati),
#       "valuta" (importi CHF, allineati a destra). Le colonne tecniche interne
#       (lat, lon, fonte_distanza) non vengono esportate: non sono utili all'utente
#       e appesantirebbero la tabella con altre colonne da scorrere.
COLONNE_EXCEL = [
    ("nome", "Struttura", "testo", 18, 34),
    ("periodo", "Periodo soggiorno", "testo", 16, 24),
    ("categoria_ricerca", "Categoria ricerca", "centro", 12, 20),
    ("tipo", "Tipo", "testo", 10, 18),
    ("descrizione", "Descrizione", "testo", 20, 42),
    ("categoria", "Categoria", "centro", 9, 11),
    ("regione", "Regione / Zona", "testo", 14, 24),
    ("valutazione_testo", "Valutazione", "centro", 10, 12),
    ("costo_hotel_chf", "Tot. hotel", "valuta", 12, 15),
    ("link", "Sito/Prenotazione", "link", 14, 20),
    ("link_maps", "Google Maps", "link", 12, 18),
    ("servizi", "Servizi", "centro", 14, 40),
    ("ore_guida", "Ore di guida", "centro", 10, 12),
    ("distanza_km", "Distanza (km)", "centro", 11, 14),
    ("prezzo_notte_chf", "Prezzo/notte", "valuta", 12, 16),
    ("costo_benzina_chf", "Benzina", "valuta", 11, 14),
    ("costo_pedaggio_chf", "Pedaggio", "valuta", 11, 14),
    ("costo_vignetta_chf", "Vignetta", "valuta", 11, 14),
    ("costo_viaggio_chf", "Tot. viaggio", "valuta", 12, 15),
    ("budget_globale_chf", "Budget totale", "valuta", 13, 17),
    ("fonte_prezzo", "Fonte prezzo", "testo", 18, 34),
]

# Palette tenue e omogenea per l'intera tabella
EXCEL_COLORE_HEADER_BG = "#5B8FB9"
EXCEL_COLORE_HEADER_TXT = "#FFFFFF"
EXCEL_COLORE_BANDA_PARI = "#EAF2F9"
EXCEL_COLORE_BANDA_DISPARI = "#FFFFFF"
EXCEL_COLORE_BORDO = "#C9D6E3"
EXCEL_RIGA_ALTEZZA_BASE = 15


def _excel_valore_cella(row, chiave):
    """Converte il valore grezzo del DataFrame nella stringa da mostrare in Excel."""
    val = row.get(chiave)
    if chiave in ("link", "link_maps"):
        return val if isinstance(val, str) else ""
    if chiave == "ore_guida":
        return f"{val:.1f} h" if val is not None else "⛴️ Traghetto/aereo"
    if chiave == "distanza_km":
        return f"{val:.0f} km" if val is not None else "n/d"
    if chiave.endswith("_chf"):
        return swiss_chf(val) if val is not None else "n/d"
    if chiave == "piscina":
        return "✅ Sì" if val else "❌ No"
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val)


def to_excel(df):
    """Esporta il DataFrame in .xlsx con colonne dimensionate sul contenuto, testo a
    capo, altezza riga adattata e una palette di colori tenue e omogenea."""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        workbook = writer.book
        worksheet = workbook.add_worksheet("Vacanze")
        writer.sheets["Vacanze"] = worksheet

        colonne_presenti = [c for c in COLONNE_EXCEL if c[0] in df.columns]

        base = {"border": 1, "border_color": EXCEL_COLORE_BORDO, "valign": "vcenter"}
        formati = {"header": workbook.add_format({
            **base, "bold": True, "font_color": EXCEL_COLORE_HEADER_TXT, "bg_color": EXCEL_COLORE_HEADER_BG,
            "align": "center", "valign": "vcenter", "text_wrap": True,
        })}
        for tipo in ("testo", "centro", "valuta"):
            allineamento = "left" if tipo == "testo" else ("center" if tipo == "centro" else "right")
            for banda, colore in (("pari", EXCEL_COLORE_BANDA_PARI), ("dispari", EXCEL_COLORE_BANDA_DISPARI)):
                formati[(tipo, banda)] = workbook.add_format({
                    **base, "align": allineamento, "bg_color": colore, "text_wrap": True,
                })
        for banda, colore in (("pari", EXCEL_COLORE_BANDA_PARI), ("dispari", EXCEL_COLORE_BANDA_DISPARI)):
            formati[("link", banda)] = workbook.add_format({
                **base, "align": "center", "bg_color": colore, "text_wrap": True,
                "font_color": "#1155CC", "underline": 1,
            })

        # Intestazione
        for col_idx, (chiave, intestazione, tipo, w_min, w_max) in enumerate(colonne_presenti):
            worksheet.write(0, col_idx, intestazione, formati["header"])

        # Larghezze colonne: dimensionate sul contenuto più lungo, entro un min/max
        # per restare omogenee e leggibili senza scorrimento orizzontale eccessivo
        larghezze = []
        for col_idx, (chiave, intestazione, tipo, w_min, w_max) in enumerate(colonne_presenti):
            valori = [_excel_valore_cella(row, chiave) for _, row in df.iterrows()]
            lunghezza_max = max([len(intestazione)] + [len(v) for v in valori])
            larghezza = min(w_max, max(w_min, lunghezza_max + 2))
            larghezze.append(larghezza)
            worksheet.set_column(col_idx, col_idx, larghezza)

        # Corpo tabella: valori + altezza riga calcolata sul testo più lungo tra le colonne
        # "testo" e sulla colonna "servizi" (che può contenere molte icone in sequenza)
        for row_idx, (_, row) in enumerate(df.iterrows(), start=1):
            banda = "pari" if row_idx % 2 == 0 else "dispari"
            righe_necessarie = 1
            for col_idx, (chiave, intestazione, tipo, w_min, w_max) in enumerate(colonne_presenti):
                valore = _excel_valore_cella(row, chiave)
                if tipo == "link":
                    testo_link = "🔗 Vedi struttura" if chiave == "link" else "🗺️ Apri in Maps"
                    if valore and valore.startswith(("http://", "https://")):
                        worksheet.write_url(row_idx, col_idx, valore, formati[(tipo, banda)], string=testo_link)
                    else:
                        worksheet.write(row_idx, col_idx, "n/d", formati[(tipo, banda)])
                else:
                    worksheet.write(row_idx, col_idx, valore, formati[(tipo, banda)])
                if (tipo == "testo" or chiave == "servizi") and larghezze[col_idx] > 0 and valore:
                    righe_necessarie = max(righe_necessarie, math.ceil(len(valore) / larghezze[col_idx]))
            worksheet.set_row(row_idx, EXCEL_RIGA_ALTEZZA_BASE * max(1, righe_necessarie))

        worksheet.set_row(0, EXCEL_RIGA_ALTEZZA_BASE * 2)
        worksheet.freeze_panes(1, 0)
        worksheet.autofilter(0, 0, len(df), len(colonne_presenti) - 1)

    return output.getvalue()


# --- PROTEZIONE PASSWORD ---
def check_password():
    """Restituisce True se l'utente ha inserito la password corretta."""
    def password_entered():
        if st.session_state["password"] == "Svizzera2026": # Cambia con la tua password
            st.session_state["password_correct"] = True
        else:
            st.session_state["password_correct"] = False
            st.error("Password errata")

    if "password_correct" not in st.session_state:
        st.text_input("Inserisci la password per accedere:", type="password", on_change=password_entered, key="password")
        return False
    elif not st.session_state["password_correct"]:
        st.text_input("Inserisci la password per accedere:", type="password", on_change=password_entered, key="password")
        return False
    else:
        return True

# --- CONTROLLO DI ACCESSO ---
st.title("🧳 Cerca Vacanze")

if check_password():
    st.subheader("Filtra le mete, calcola budget completi e visualizza i risultati")

    st.info(
        "ℹ️ **Come iniziare**: apri i passaggi nella sidebar uno alla volta (parti da '1️⃣ Chi "
        "viaggia'), poi attiva '5️⃣ Ricerca geografica in tempo reale' e premi '🔍 Cerca hotel nelle "
        "zone' per ottenere hotel realmente disponibili, con prezzi, km ed ore di guida reali. Se "
        "vuoi prima farti un'idea di come appare la tabella dei risultati, puoi anche vedere "
        "un'anteprima con dati fittizi qui sotto, senza consumare nessuna ricerca.",
        icon="ℹ️",
    )

    # --- SIDEBAR: CRITERI DI RICERCA, in 5 passaggi numerati e comprimibili -----------------
    # Solo il primo passo è aperto di default: gli altri restano ripiegati finché l'utente non
    # li apre volontariamente, per non mostrare tutti i controlli insieme fin dall'inizio.
    st.sidebar.header("Imposta i tuoi filtri")
    st.sidebar.caption("Procedi un passo alla volta: apri una sezione, poi passa alla successiva.")

    with st.sidebar.expander("1️⃣ Chi viaggia", expanded=True):
        origen = st.text_input("Città di partenza:", value="Lugano")

        st.markdown("**Membri del viaggio:**")
        adulti = st.number_input("Adulti", min_value=1, max_value=10, value=3)
        bambini = st.number_input("Bambini", min_value=0, max_value=10, value=1)

        eta_bambini = []
        if bambini > 0:
            st.caption(
                "Google Hotels richiede l'età di ogni bambino per calcolare il prezzo corretto "
                "(letti extra, tariffe family, ecc.). Obbligatoria per la ricerca geografica."
            )
            cols_eta = st.columns(min(int(bambini), 4) or 1)
            for i in range(int(bambini)):
                with cols_eta[i % len(cols_eta)]:
                    eta = st.number_input(
                        f"Età bimbo {i + 1}", min_value=0, max_value=17, value=8, key=f"eta_bambino_{i}"
                    )
                eta_bambini.append(eta)

    with st.sidebar.expander("2️⃣ Limiti di viaggio e budget"):
        nessun_limite_ore = st.checkbox(
            "Nessun limite di tempo massimo in auto", value=False,
            help="Utile per includere mete con un percorso stradale reale ma lunghissimo (es. una "
                 "rotta che nei dati OpenStreetMap include una linea traghetto): con il limite attivo "
                 "verrebbero comunque escluse, perché tecnicamente 'raggiungibili in auto', solo con "
                 "un tempo enorme."
        )
        if nessun_limite_ore:
            ore_auto_max = math.inf
            st.caption("↑ Limite disattivato: nessun hotel verrà escluso in base al tempo di guida.")
        else:
            ore_auto_max = st.slider("Tempo massimo in auto (ore):", min_value=1.0, max_value=12.0, value=5.0, step=0.5)
        giorni_soggiorno = st.slider("Durata soggiorno (giorni):", min_value=4, max_value=14, value=7)
        st.caption("↑ Questo limite è sempre istantaneo: le ore di guida sono già note per ogni hotel trovato.")

        includi_non_raggiungibili = st.checkbox(
            "Includi anche mete non raggiungibili in auto (isole, traghetto/aereo necessario)",
            value=False,
            help="Se disattivato (default), una meta per cui non esiste alcun percorso stradale "
                 "(es. un'isola) viene sempre esclusa, qualunque sia il limite di ore impostato sopra. "
                 "Attivalo per includerla comunque: in tal caso il limite di ore in auto non le si "
                 "applica, e costi di benzina/pedaggio/vignetta non vengono calcolati per lei (mostrati "
                 "come 'n/d'), perché presuppongono un viaggio in auto che qui non esiste."
        )
        if includi_non_raggiungibili:
            st.caption(
                "⚠️ Per le mete raggiungibili solo via traghetto/aereo, budget mostrato = solo costo "
                "hotel (nessuna stima di viaggio); il limite di ore massime in auto viene ignorato "
                "solo per queste mete, non per quelle raggiungibili normalmente in auto."
            )

        budget_max_chf = st.number_input(
            "Budget massimo hotel (CHF, 0 = nessun limite):", min_value=0, value=0, step=10,
        )
        tipo_budget = "Prezzo a notte"
        if budget_max_chf > 0:
            tipo_budget = st.radio(
                "Il budget si riferisce a:", ["Prezzo a notte", "Costo totale soggiorno"],
                horizontal=True,
            )
        st.caption(
            "↑ Anche questo limite è sempre istantaneo: filtra localmente sul prezzo già noto di ogni "
            "hotel, sia nel database di esempio sia nei risultati di una ricerca geografica."
        )

    with st.sidebar.expander("3️⃣ Filtri di ricerca"):
        st.warning(
            "**Come funzionano**: questi filtri vengono inviati a Google Hotels *dentro* la richiesta di "
            "ricerca, così è Google stesso a restituire solo hotel già pertinenti (più preciso che "
            "scaricare un campione e scartarlo dopo). Di conseguenza:\n"
            "- Impostali **prima** di premere '🔍 Cerca hotel nelle zone'.\n"
            "- Se li cambi **dopo** una ricerca già fatta, non hanno alcun effetto sui risultati mostrati "
            "finché non premi di nuovo il pulsante — non filtrano localmente.\n"
            "- Sul database di esempio (prima di ogni ricerca) restano invece istantanei, perché lì non "
            "scatta mai una vera chiamata a Google Hotels.",
            icon="⚠️",
        )
        piscina_obbligatoria = st.checkbox("Piscina", value=True)
        wifi_obbligatorio = st.checkbox("WiFi gratuito", value=False)
        colazione_obbligatoria = st.checkbox("Colazione inclusa", value=False)
        parcheggio_obbligatorio = st.checkbox("Parcheggio gratuito", value=False)
        animali_obbligatorio = st.checkbox("Animali ammessi", value=False)
        cancellazione_obbligatoria = st.checkbox("Cancellazione gratuita", value=False)
        categoria_minima = st.select_slider(
            "Categoria minima (stelle):", options=[0, 1, 2, 3, 4, 5], value=0,
            help="0 = nessun filtro."
        )
        valutazione_minima = st.select_slider(
            "Valutazione minima (Google):", options=[0, 3.5, 4.0, 4.5], value=0,
            help="0 = nessun filtro. Google Hotels supporta solo queste 3 soglie fisse, non un valore libero."
        )
        st.markdown("**➕ Altri servizi:**")
        palestra_obbligatoria = st.checkbox("Palestra", value=False)
        ristorante_obbligatorio = st.checkbox("Ristorante in struttura", value=False)
        spa_obbligatoria = st.checkbox("Spa", value=False)
        bar_obbligatorio = st.checkbox("Bar", value=False)
        servizio_camera_obbligatorio = st.checkbox("Servizio in camera", value=False)
        aria_condizionata_obbligatoria = st.checkbox("Aria condizionata", value=False)
        accesso_spiaggia_obbligatorio = st.checkbox("Accesso spiaggia", value=False)
        bambini_obbligatorio = st.checkbox("Adatto a bambini", value=False)
        all_inclusive_obbligatorio = st.checkbox("All-inclusive", value=False)
        accessibile_obbligatorio = st.checkbox("Accessibile in sedia a rotelle", value=False)
        ricarica_ev_obbligatoria = st.checkbox("Colonnina ricarica auto elettrica", value=False)

        st.markdown("**🔍 Rifiniture locali (istantanee):**")
        st.caption(
            "Queste invece si applicano subito sugli hotel già trovati, senza bisogno di una nuova "
            "ricerca — perché Google Hotels non le supporta come filtro nella richiesta, quindi le "
            "verifico io leggendo la descrizione di ogni hotel già scaricato."
        )
        vicino_sci_obbligatorio = st.checkbox("Vicino agli impianti di risalita", value=False)
        deposito_sci_obbligatorio = st.checkbox("Deposito / noleggio sci", value=False)
        scuola_sci_obbligatoria = st.checkbox("Scuola sci", value=False)

        st.markdown("**🌊 Distanza da mare / lago (automatico, istantaneo):**")
        st.caption(
            "A differenza del punto di riferimento manuale qui sotto, questo filtro calcola per "
            "OGNI hotel la distanza dal tratto di costa o dalla riva del lago più vicino a **quel** "
            "hotel specifico (tramite OpenStreetMap) — quindi funziona correttamente anche cercando "
            "contemporaneamente in più regioni con mari/laghi diversi tra loro. La distanza è km/tempo "
            "di guida REALI (OSRM), non linea d'aria, con fallback automatico a stima lineare se OSRM "
            "non risponde."
        )
        vicino_mare_attivo = st.checkbox("Vicino al mare", value=False)
        unita_mare = "Km reali in auto"
        distanza_mare_max_km = 5.0
        tempo_mare_max_min = 10
        if vicino_mare_attivo:
            unita_mare = st.radio(
                "Misura la distanza dal mare in:", ["Km reali in auto", "Minuti di guida"],
                horizontal=True, key="unita_mare",
            )
            if unita_mare == "Km reali in auto":
                distanza_mare_max_km = st.slider(
                    "Distanza massima dalla costa (km in auto):", min_value=0.5, max_value=20.0, value=5.0, step=0.5
                )
            else:
                tempo_mare_max_min = st.slider(
                    "Tempo massimo dalla costa (minuti di guida):", min_value=2, max_value=60, value=10, step=1
                )
        vicino_lago_attivo = st.checkbox("Vicino a un lago", value=False)
        unita_lago = "Km reali in auto"
        distanza_lago_max_km = 5.0
        tempo_lago_max_min = 10
        if vicino_lago_attivo:
            unita_lago = st.radio(
                "Misura la distanza dal lago in:", ["Km reali in auto", "Minuti di guida"],
                horizontal=True, key="unita_lago",
            )
            if unita_lago == "Km reali in auto":
                distanza_lago_max_km = st.slider(
                    "Distanza massima dal lago (km in auto):", min_value=0.5, max_value=20.0, value=5.0, step=0.5
                )
            else:
                tempo_lago_max_min = st.slider(
                    "Tempo massimo dal lago (minuti di guida):", min_value=2, max_value=60, value=10, step=1
                )
        if vicino_mare_attivo or vicino_lago_attivo:
            st.caption(
                "⚠️ Interroga OpenStreetMap + OSRM per ogni hotel superstite agli altri filtri: con "
                "molti risultati può rallentare visibilmente il caricamento della tabella (i risultati "
                "vengono comunque tenuti in cache, i rerun successivi sono istantanei)."
            )

        st.markdown("**📍 Distanza da un punto di riferimento specifico (manuale, istantaneo):**")
        punto_riferimento_nome = st.text_input(
            "Nome del luogo (es. 'Spiaggia di Riccione', 'Gardaland', 'Duomo di Milano'):", value="",
            help="Lascia vuoto per non applicare questo filtro. A differenza del filtro 'mare/lago' "
                 "sopra, qui il riferimento è UN SOLO punto fisso uguale per tutti gli hotel: ha senso "
                 "solo se le zone che stai esplorando sono tutte vicine tra loro e a questo punto. Se "
                 "cerchi in regioni molto distanti (es. Liguria + Emilia Romagna) con un punto specifico "
                 "di una sola di esse, rischi di escludere per errore risultati validi nelle altre zone."
        )
        unita_punto = "Km reali in auto"
        distanza_punto_max_km = 15.0
        tempo_punto_max_min = 20
        if punto_riferimento_nome.strip():
            unita_punto = st.radio(
                "Misura la distanza dal punto in:", ["Km reali in auto", "Minuti di guida"],
                horizontal=True, key="unita_punto",
            )
            if unita_punto == "Km reali in auto":
                distanza_punto_max_km = st.slider(
                    "Distanza massima dal punto (km in auto):", min_value=1.0, max_value=150.0, value=15.0, step=1.0
                )
            else:
                tempo_punto_max_min = st.slider(
                    "Tempo massimo dal punto (minuti di guida):", min_value=2, max_value=180, value=20, step=1
                )
            st.caption("↑ Km/tempo di guida REALI (OSRM), non linea d'aria.")

    # Mappe (flag selezionato dall'utente -> chiave del servizio) riusate sia per costruire la
    # richiesta a SerpApi sia per filtrare localmente il database di esempio.
    MAPPA_FILTRI_AMENITA = [
        (piscina_obbligatoria, "piscina"), (wifi_obbligatorio, "wifi"),
        (colazione_obbligatoria, "colazione"), (parcheggio_obbligatorio, "parcheggio"),
        (animali_obbligatorio, "animali"), (palestra_obbligatoria, "palestra"),
        (ristorante_obbligatorio, "ristorante"), (spa_obbligatoria, "spa"),
        (bar_obbligatorio, "bar"), (servizio_camera_obbligatorio, "servizio_camera"),
        (aria_condizionata_obbligatoria, "aria_condizionata"),
        (accesso_spiaggia_obbligatorio, "accesso_spiaggia"), (bambini_obbligatorio, "bambini"),
        (all_inclusive_obbligatorio, "all_inclusive"), (accessibile_obbligatorio, "accessibile"),
        (ricarica_ev_obbligatoria, "ricarica_ev"),
    ]
    MAPPA_FILTRI_LOCALI = [
        (vicino_sci_obbligatorio, "vicino_sci"), (deposito_sci_obbligatorio, "deposito_sci"),
        (scuola_sci_obbligatoria, "scuola_sci"),
    ]

    usa_ricerca_geografica = False
    serpapi_key = ""
    data_checkin = date.today() + timedelta(days=30)
    zone_testo = "Riva del Garda\nFinale Ligure\nSan Gimignano"
    max_hotel_per_zona = 10
    ordinamento_scelto = "Rilevanza (Google)"
    CODICI_ORDINAMENTO_SERPAPI = {
        "Rilevanza (Google)": None, "Prezzo più basso": 3,
        "Valutazione più alta": 8, "Più recensiti": 13,
    }
    avvia_ricerca = False
    cerca_bnb = False
    camere_minime = 0
    bagni_minimi = 0

    with st.sidebar.expander("4️⃣ Costi di viaggio e pedaggi"):
        st.markdown("**🚗 Costi di Viaggio (A/R):**")
        costo_benzina = st.number_input("Costo carburante (CHF/litro)", value=1.80, step=0.05)
        consumo_medio = st.number_input("Consumo medio (litri/100km)", value=7.0, step=0.5)

        st.markdown("**🛣️ Pedaggi autostradali**")
        st.caption(
            "Non essendoci un calcolo casello-per-casello disponibile, usiamo la tariffa media ufficiale "
            "per veicoli di Classe A (auto, aggiornata al 1° gennaio 2026, IVA inclusa) applicata ai km "
            "reali del percorso (ora calcolati da OSRM quando la ricerca geografica è attiva)."
        )
        tariffa_pedaggio_eur_km = st.slider(
            "Tariffa media pedaggio (EUR/km, Classe A):",
            min_value=0.05, max_value=0.15, value=0.085, step=0.005,
            help="Le tratte di pianura costano in media 0,07-0,08 EUR/km, quelle di montagna "
                 "(gallerie, viadotti, es. Brennero) fino a 0,10-0,13 EUR/km."
        )
        quota_km_autostrada = st.slider(
            "% del percorso realmente in autostrada a pedaggio:",
            min_value=50, max_value=100, value=90, step=5,
            help="Gli ultimi km per raggiungere l'hotel spesso sono su strade extraurbane non a pedaggio."
        )
        cambio_eur_chf = st.number_input(
            "Cambio EUR → CHF (CHF per 1 EUR):", value=0.93, step=0.01,
            help="Verifica il cambio attuale: variazioni anche piccole incidono sul costo del pedaggio."
        )

        st.markdown("**🇨🇭 Vignetta autostradale svizzera**")
        ha_gia_vignetta = st.checkbox(
            "Possiedo già la vignetta svizzera valida", value=False,
            help="La vignetta annuale svizzera costa CHF 40.- (obbligatoria per percorrere anche solo un "
                 "breve tratto di autostrada/semi-autostrada svizzera, es. Lugano-Chiasso)."
        )
        VIGNETTA_SVIZZERA_CHF = 40.0

    with st.sidebar.expander("5️⃣ Ricerca geografica in tempo reale (opzionale)"):
        st.caption(
            "Cerca hotel realmente disponibili in una o più zone tramite Google Hotels ([SerpApi]"
            "(https://serpapi.com/manage-api-key), 250 ricerche/mese gratuite), calcolando km e ore di "
            "guida reali da OSRM (motore di instradamento gratuito). I prezzi mostrati includono le "
            "eventuali commissioni di Booking/Expedia/ecc., perché sono quelli reali pubblicati online."
        )
        usa_ricerca_geografica = st.checkbox("Attiva ricerca geografica", value=False)

        if usa_ricerca_geografica:
            with st.popover("🔑 Dove trovo la mia SerpApi API Key?"):
                st.markdown(
                    "**Promemoria — da rileggere ogni volta che serve:**\n\n"
                    "1. Vai su **[serpapi.com/dashboard](https://serpapi.com/dashboard)** e accedi "
                    "(anche con Google, se l'avevi usato la prima volta).\n"
                    "2. Nella pagina **'Your Account'**, cerca il riquadro **'Your Private API Key'** "
                    "(di solito è già visibile appena entri, verso metà pagina).\n"
                    "3. Clicca l'icona del blocco appunti 📋 accanto alla stringa lunga di lettere e "
                    "numeri per copiarla (oppure selezionala a mano e Ctrl+C / Cmd+C).\n"
                    "4. Torna qui e incollala nel campo **'SerpApi API Key'** qui sotto (Ctrl+V / Cmd+V).\n\n"
                    "⚠️ La chiave non scade mai da sola, ma questa app **non la salva**: se non la usi per "
                    "un po' e il campo è vuoto, ripeti questi 4 passaggi. Il piano gratuito include 250 "
                    "ricerche al mese, che si azzerano automaticamente (le vedi in alto nella dashboard, "
                    "'0 / 250 searches')."
                )
            serpapi_key = st.text_input("SerpApi API Key", type="password")

            if serpapi_key:
                quota, errore_quota = controlla_quota_serpapi(serpapi_key)
                if errore_quota:
                    st.caption(f"⚠️ Impossibile leggere la quota residua: {errore_quota}")
                elif quota:
                    limite = quota["limite_mensile"]
                    usate = quota["usate_questo_mese"]
                    rimanenti = quota["rimanenti"]
                    extra = quota["crediti_extra"] or 0
                    if limite and usate is not None:
                        st.progress(
                            min(1.0, usate / limite),
                            text=f"📊 {usate} / {limite} ricerche usate questo mese",
                        )
                    if rimanenti is not None:
                        totale_disponibile = rimanenti + extra
                        if totale_disponibile <= 20:
                            st.error(f"🔴 Solo {totale_disponibile} ricerche rimaste (piano + extra)!")
                        elif totale_disponibile <= 50:
                            st.warning(f"🟠 {totale_disponibile} ricerche rimaste (piano + extra).")
                        else:
                            st.caption(f"🟢 {totale_disponibile} ricerche rimaste (piano + extra).")
                    if extra:
                        st.caption(f"↳ di cui {extra} crediti extra.")

            data_checkin = st.date_input("Data check-in", value=data_checkin, min_value=date.today())
            zone_testo = st.text_area(
                "Luoghi da esplorare (uno per riga):", value=zone_testo, height=100,
                help="⚠️ Scrivi un LUOGO (città, zona, comprensorio, regione) — NON il nome di un hotel "
                     "specifico. Per ogni riga, Google Hotels ti propone in autonomia fino al numero di "
                     "hotel scelto qui sotto, realmente disponibili in quel luogo, con prezzo reale. "
                     "Es. 'Cortina d'Ampezzo', 'Costa Azzurra', 'Chianti'. Più righe aggiungi, più "
                     "ricerche vengono consumate (1 per riga)."
            )
            max_hotel_per_zona = st.slider(
                "Hotel da scaricare per zona:", min_value=5, max_value=20, value=10,
                help="Quanti risultati prendere da ciascuna zona, nell'ordine scelto qui sotto. Un "
                     "numero più alto copre meglio la zona ma la risposta è un po' più pesante da "
                     "scaricare; non consuma comunque più di 1 ricerca per zona."
            )
            ordinamento_scelto = st.selectbox(
                "Ordina i risultati per:",
                options=["Rilevanza (Google)", "Prezzo più basso", "Valutazione più alta", "Più recensiti"],
                index=0,
                help="Determina QUALI hotel finiscono tra i primi N scaricati da ciascuna zona (non solo "
                     "l'ordine in tabella). 'Rilevanza' è il criterio di default di Google — un mix di "
                     "popolarità, prezzo e posizionamento che Google non rende pubblico nel dettaglio."
            )

            st.markdown("**🏡 B&B e case vacanza**")
            cerca_bnb = st.checkbox(
                "Cerca anche B&B / case vacanza", value=False,
                help="Oltre agli hotel, interroga Google Hotels anche in modalità 'Vacation Rentals' "
                     "(B&B, appartamenti, case vacanza). È una seconda ricerca indipendente per ogni "
                     "zona, quindi RADDOPPIA le ricerche SerpApi consumate ad ogni click."
            )
            camere_minime = 0
            bagni_minimi = 0
            if cerca_bnb:
                st.warning(
                    "⚠️ Ogni zona verrà interrogata due volte (hotel + B&B/case vacanza): il consumo di "
                    "ricerche SerpApi per questo click raddoppia. Inoltre Google Hotels non supporta, per "
                    "le case vacanza, i filtri 'categoria stelle' e 'cancellazione gratuita': verranno "
                    "applicati solo a hotel e ignorati per i risultati B&B (te lo segnalo negli avvisi "
                    "dopo la ricerca).",
                    icon="⚠️",
                )
                col_camere, col_bagni = st.columns(2)
                with col_camere:
                    camere_minime = st.number_input("Camere min.", min_value=0, max_value=10, value=0)
                with col_bagni:
                    bagni_minimi = st.number_input("Bagni min.", min_value=0, max_value=10, value=0)
                st.caption("↑ Solo per B&B/case vacanza (0 = nessun limite). Non si applica agli hotel.")

            avvia_ricerca = st.button("🔍 Cerca hotel nelle zone", disabled=not serpapi_key)
            if not serpapi_key:
                st.warning("Inserisci la tua API key per attivare la ricerca.")

            ultima_ricerca = st.session_state.get("ricerca_geo_timestamp")
            if ultima_ricerca:
                n_hotel = len(st.session_state.get("hotel_trovati", []))
                st.caption(f"🕒 Ultima ricerca: {ultima_ricerca.strftime('%d.%m.%Y alle %H:%M')} — {n_hotel} hotel trovati")
            else:
                st.caption("Nessuna ricerca ancora effettuata.")

            st.caption(
                "⚠️ Cambiare '3️⃣ Filtri di ricerca' **non** aggiorna da solo i risultati mostrati (vedi "
                "nota sopra): serve premere di nuovo questo pulsante. Cambiare invece '2️⃣ Limiti di "
                "viaggio' o le 'Rifiniture locali' aggiorna la tabella all'istante, senza consumare una "
                "nuova ricerca."
            )

    with st.sidebar.expander("📊 Utilizzo delle API in questa sessione"):
        st.caption(
            "Conteggio delle chiamate di rete REALMENTE eseguite verso ciascun servizio esterno in "
            "questa sessione (si azzera riavviando l'app). Le richieste già servite dalla cache non "
            "vengono contate qui, perché non generano alcun nuovo traffico verso il servizio."
        )
        contatori = st.session_state.get("contatore_chiamate", {})

        st.markdown("**🔒 Con quota gratuita mensile (a pagamento oltre soglia):**")
        st.caption(
            "SerpApi (Google Hotels) — 250 ricerche/mese incluse nel piano gratuito, poi a pagamento. "
            "Il conteggio dettagliato (usate/rimanenti) è nel passo '5️⃣ Ricerca geografica' qui sopra, "
            "appena inserisci la tua API key."
        )

        st.markdown("**🌍 Gratuiti, senza quota mensile (limitati solo dal ritmo delle richieste):**")
        st.write(f"- **Nominatim** (geocodifica città/punti): {contatori.get('nominatim', 0)} chiamate")
        st.caption("↳ Policy OpenStreetMap: max ~1 richiesta al secondo consigliata, nessun tetto mensile.")
        st.write(f"- **OSRM** (km/tempo di guida reali): {contatori.get('osrm', 0)} chiamate")
        st.caption("↳ Server demo pubblico gratuito: nessun limite dichiarato, ma non garantito per uso intensivo.")
        st.write(f"- **Overpass** (ricerca costa/lago più vicino): {contatori.get('overpass', 0)} chiamate")
        st.caption("↳ Nessun limite mensile fisso; timeout 15s per query, può rallentare in caso di uso intensivo.")

        if st.button("🔄 Azzera contatori di questa sessione"):
            st.session_state["contatore_chiamate"] = {}
            st.rerun()

    # --- DATABASE DI ESEMPIO (usato solo finché non si esegue una ricerca geografica) ---
    # I campi wifi/colazione/parcheggio/animali/vicino_sci/cancellazione/categoria/valutazione
    # sono valori plausibili scritti a mano, dato che questo database statico non proviene da
    # nessuna fonte reale (a differenza degli hotel trovati con la ricerca geografica).
    def _servizi_mock(**presenti):
        """Parte da 'nessun servizio' e attiva solo quelli passati come True, per non dover
        scrivere tutti e 20 i campi per ogni hotel di esempio."""
        base = {chiave: False for chiave in PAROLE_CHIAVE_SERVIZI}
        base.update(presenti)
        return base

    destinazioni_mock = [
        {"nome": "Hotel Savoy Palace (4★) - Riva del Garda", "regione": "Trentino (Lago)", "ore_guida": 3.5, "distanza_km": 250, "lat": 45.8893, "lon": 10.8431,
         **_servizi_mock(piscina=True, wifi=True, colazione=True, parcheggio=True, palestra=True, ristorante=True, spa=True, bar=True, servizio_camera=True, aria_condizionata=True, bambini=True, accessibile=True, ricarica_ev=True),
         "cancellazione_gratuita": True, "categoria_stelle": 4, "valutazione": 4.5, "prezzo_notte_chf": 620, "descrizione": "Resort con grandi piscine.", "tipo": "Lago / Relax"},
        {"nome": "Albergo Deva (3★) - Riva del Garda", "regione": "Trentino (Lago)", "ore_guida": 3.5, "distanza_km": 250, "lat": 45.9100, "lon": 10.8200,
         **_servizi_mock(piscina=True, wifi=True, colazione=True, parcheggio=True, animali=True, ristorante=True, bar=True, aria_condizionata=True, bambini=True),
         "cancellazione_gratuita": False, "categoria_stelle": 3, "valutazione": 4.0, "prezzo_notte_chf": 280, "descrizione": "Hotel con vista.", "tipo": "Lago / Economica"},
        {"nome": "Hotel Careni Villa Italia (3★) - Finale Ligure", "regione": "Liguria (Mare)", "ore_guida": 3.2, "distanza_km": 278, "lat": 44.1741, "lon": 8.3537,
         **_servizi_mock(piscina=True, wifi=True, ristorante=True, bar=True, accesso_spiaggia=True),
         "cancellazione_gratuita": True, "categoria_stelle": 3, "valutazione": 4.2, "prezzo_notte_chf": 187, "descrizione": "Elegante edificio anni 30.", "tipo": "Mare / Spiaggia"},
        {"nome": "Relais Santa Chiara (4★) - San Gimignano", "regione": "Toscana (Campagna)", "ore_guida": 4.8, "distanza_km": 436, "lat": 43.4672, "lon": 11.0434,
         **_servizi_mock(piscina=True, wifi=True, colazione=True, parcheggio=True, palestra=True, ristorante=True, spa=True, bar=True, servizio_camera=True, aria_condizionata=True, accessibile=True, ricarica_ev=True),
         "cancellazione_gratuita": True, "categoria_stelle": 4, "valutazione": 4.6, "prezzo_notte_chf": 254, "descrizione": "Immerso nel verde.", "tipo": "Campagna / Relax"},
        {"nome": "Camping Il Boschetto di Piemma (3★) - San Gimignano", "regione": "Toscana (Campagna)", "ore_guida": 4.8, "distanza_km": 436, "lat": 43.4550, "lon": 11.0500,
         **_servizi_mock(piscina=True, parcheggio=True, animali=True, bambini=True),
         "cancellazione_gratuita": False, "categoria_stelle": 3, "valutazione": 3.8, "prezzo_notte_chf": 123, "descrizione": "Soluzione natura.", "tipo": "Campagna / Economica"}
    ]
    for d in destinazioni_mock:
        d["fonte_prezzo"] = "⚪ Stima statica (dati di esempio)"
        d["fonte_distanza"] = "Stima statica (dati di esempio)"
        d["categoria_ricerca"] = "Hotel"
        d["link"] = ""

    # --- ESECUZIONE RICERCA GEOGRAFICA (solo al click del pulsante) ---
    data_checkout = data_checkin + timedelta(days=giorni_soggiorno)

    if usa_ricerca_geografica and avvia_ricerca and serpapi_key:
        amenita_selezionate = [AMENITA_ID_SERPAPI[chiave] for flag, chiave in MAPPA_FILTRI_AMENITA if flag]

        filtri_extra = {}
        if amenita_selezionate:
            filtri_extra["amenities"] = ",".join(str(a) for a in amenita_selezionate)
        if cancellazione_obbligatoria:
            filtri_extra["free_cancellation"] = "true"
        if categoria_minima > 0:
            filtri_extra["hotel_class"] = ",".join(str(c) for c in range(categoria_minima, 6))
        if valutazione_minima and valutazione_minima > 0:
            codice_rating = RATING_CODICI_SERPAPI.get(valutazione_minima)
            if codice_rating:
                filtri_extra["rating"] = codice_rating
        codice_ordinamento = CODICI_ORDINAMENTO_SERPAPI.get(ordinamento_scelto)
        if codice_ordinamento is not None:
            filtri_extra["sort_by"] = codice_ordinamento

        # Filtri per la ricerca B&B/case vacanza: Google Hotels non supporta, per le case vacanza,
        # 'hotel_class', 'free_cancellation' e le 'amenities' con gli stessi ID usati per gli hotel
        # (l'elenco ID è diverso e non verificato), quindi questi tre non vengono inviati. 'rating'
        # e 'sort_by' invece sono generici e valgono per entrambe le modalità.
        filtri_extra_bnb = {"vacation_rentals": "true"}
        filtri_bnb_ignorati = []
        if amenita_selezionate:
            filtri_bnb_ignorati.append("servizi/amenità selezionati")
        if cancellazione_obbligatoria:
            filtri_bnb_ignorati.append("cancellazione gratuita")
        if categoria_minima > 0:
            filtri_bnb_ignorati.append("categoria minima (stelle)")
        if valutazione_minima and valutazione_minima > 0 and codice_rating:
            filtri_extra_bnb["rating"] = codice_rating
        if codice_ordinamento is not None:
            filtri_extra_bnb["sort_by"] = codice_ordinamento
        if camere_minime > 0:
            filtri_extra_bnb["bedrooms"] = int(camere_minime)
        if bagni_minimi > 0:
            filtri_extra_bnb["bathrooms"] = int(bagni_minimi)

        with st.spinner("Geocodifica della città di partenza..."):
            origine_coord = geocodifica_citta(origen)

        if origine_coord is None:
            st.sidebar.error(f"Non sono riuscito a geolocalizzare '{origen}'. Controlla il nome della città.")
        else:
            zone = [z.strip() for z in zone_testo.splitlines() if z.strip()]
            spinner_testo = f"Ricerca hotel in {len(zone)} zone su Google Hotels e calcolo distanze reali..."
            if cerca_bnb:
                spinner_testo = f"Ricerca hotel e B&B/case vacanza in {len(zone)} zone (2 ricerche/zona) e calcolo distanze reali..."
            with st.spinner(spinner_testo):
                hotel_trovati, avvisi = esegui_ricerca_geografica(
                    origine_coord[0], origine_coord[1], zone,
                    data_checkin, data_checkout, adulti, bambini, serpapi_key,
                    filtri_extra=filtri_extra, max_risultati=max_hotel_per_zona,
                    eta_bambini=eta_bambini, categoria_ricerca="Hotel",
                )
                if cerca_bnb:
                    if filtri_bnb_ignorati:
                        avvisi.append(
                            "B&B/case vacanza: filtri ignorati perché non supportati da Google per "
                            "questa modalità → " + ", ".join(filtri_bnb_ignorati) + "."
                        )
                    bnb_trovati, avvisi_bnb = esegui_ricerca_geografica(
                        origine_coord[0], origine_coord[1], zone,
                        data_checkin, data_checkout, adulti, bambini, serpapi_key,
                        filtri_extra=filtri_extra_bnb, max_risultati=max_hotel_per_zona,
                        eta_bambini=eta_bambini, categoria_ricerca="B&B / Casa vacanza",
                    )
                    hotel_trovati += bnb_trovati
                    avvisi += avvisi_bnb
            st.session_state["hotel_trovati"] = hotel_trovati
            st.session_state["ricerca_geo_timestamp"] = datetime.now()
            st.session_state["ricerca_geo_avvisi"] = avvisi
            st.session_state["ricerca_geo_origine"] = origen
            st.session_state["ricerca_geo_filtri"] = dict(filtri_extra)  # per mostrare cosa era attivo
            controlla_quota_serpapi.clear()  # forza il ricalcolo della quota residua al prossimo rerun

    # --- SORGENTE DATI: hotel trovati realmente, se presenti; altrimenti NESSUN risultato, a meno
    # che l'utente non chieda esplicitamente un'anteprima con dati di esempio ---
    ricerca_reale_disponibile = usa_ricerca_geografica and st.session_state.get("hotel_trovati") is not None
    if ricerca_reale_disponibile:
        sorgente_dati = st.session_state["hotel_trovati"]
    elif st.session_state.get("mostra_demo"):
        sorgente_dati = destinazioni_mock
    else:
        sorgente_dati = None
    # Se la sorgente viene da una ricerca geografica reale, i filtri server-side sono già stati
    # applicati da Google Hotels: non vanno ripetuti localmente (il dato scaricato li rispetta già).
    filtri_gia_applicati_lato_server = sorgente_dati is not None and sorgente_dati is not destinazioni_mock

    if usa_ricerca_geografica and st.session_state.get("ricerca_geo_avvisi"):
        with st.expander("⚠️ Avvisi dell'ultima ricerca geografica"):
            for avviso in st.session_state["ricerca_geo_avvisi"]:
                st.caption(avviso)

    if sorgente_dati is None:
        st.subheader("👋 Nessuna ricerca ancora effettuata")
        st.write(
            "Configura i tuoi criteri nella sidebar (parti dal passo '1️⃣ Chi viaggia') e poi attiva "
            "il passo '5️⃣ Ricerca geografica in tempo reale' per trovare hotel realmente disponibili."
        )
        if st.button("👀 Vedi un'anteprima con dati di esempio"):
            st.session_state["mostra_demo"] = True
            st.rerun()
    else:
        # Geocodifica il punto di riferimento manuale UNA SOLA VOLTA, poi calcola in un'UNICA
        # chiamata OSRM batch le distanze/tempi di guida reali verso TUTTI gli hotel (molto più
        # efficiente che una chiamata OSRM per hotel dentro al ciclo qui sotto).
        punto_riferimento_coord = None
        mappa_distanza_punto = {}
        if punto_riferimento_nome.strip():
            punto_riferimento_coord = geocodifica_citta(punto_riferimento_nome.strip())
            if punto_riferimento_coord is None:
                st.warning(
                    f"⚠️ Non sono riuscito a geolocalizzare '{punto_riferimento_nome}': il filtro di "
                    "distanza dal punto di riferimento non è stato applicato."
                )
            else:
                coordinate_hotel = tuple((d["lat"], d["lon"]) for d in sorgente_dati)
                risultati_punto = distanze_auto_da_punto(
                    punto_riferimento_coord[0], punto_riferimento_coord[1], coordinate_hotel
                )
                mappa_distanza_punto = dict(zip(coordinate_hotel, risultati_punto))

        risultati = []
        st.session_state["errori_overpass"] = []  # riparte pulito ad ogni rerun: riflette solo lo stato attuale
        diagnostica_distanze = {
            "mare_valutati": 0, "mare_sconosciuti": 0, "mare_lontani": 0,
            "lago_valutati": 0, "lago_sconosciuti": 0, "lago_lontani": 0,
            "punto_valutati": 0, "punto_sconosciuti": 0, "punto_lontani": 0,
        }
        ore_guida_oltre_limite = []  # per diagnosticare il caso "percorso stradale via traghetto molto lungo"
        esclusi_non_raggiungibili = 0
        for dest in sorgente_dati:
            raggiungibile_auto = dest.get("ore_guida") is not None
            if not raggiungibile_auto and not includi_non_raggiungibili:
                esclusi_non_raggiungibili += 1
                continue  # meta senza percorso stradale (es. isola): esclusa finché non richiesto esplicitamente
            # "ore_guida" e i filtri in MAPPA_FILTRI_LOCALI sono rifiniture locali: si applicano
            # sempre, subito, indipendentemente dalla sorgente dei dati. Il limite di ore non si
            # applica alle mete non raggiungibili in auto (già ammesse solo se l'utente lo vuole).
            entro_limite_ore = not raggiungibile_auto or dest["ore_guida"] <= ore_auto_max
            if raggiungibile_auto and not entro_limite_ore:
                # OSRM ha trovato un percorso stradale "valido" (anche se magari include un traghetto
                # nei dati OpenStreetMap): non risulta "irraggiungibile" in senso stretto, quindi il
                # checkbox 'includi non raggiungibili' non lo salva — viene scartato dal normale
                # limite di ore. Lo registriamo per poterlo segnalare in diagnostica.
                ore_guida_oltre_limite.append(dest["ore_guida"])
            rispetta_filtri = entro_limite_ore and all(
                not flag or dest.get(chiave) for flag, chiave in MAPPA_FILTRI_LOCALI
            )
            if rispetta_filtri and budget_max_chf > 0:
                prezzo_confronto = (
                    dest["prezzo_notte_chf"] if tipo_budget == "Prezzo a notte"
                    else dest["prezzo_notte_chf"] * giorni_soggiorno
                )
                rispetta_filtri = prezzo_confronto <= budget_max_chf
            # I filtri "lato server" (MAPPA_FILTRI_AMENITA + categoria + valutazione) li riapplico
            # localmente SOLO sul database di esempio (che non passa mai da Google Hotels): sui
            # risultati di una ricerca geografica reale sono già stati applicati da Google al momento
            # della richiesta, quindi non li ripeto qui.
            if rispetta_filtri and not filtri_gia_applicati_lato_server:
                rispetta_filtri = (
                    all(not flag or dest.get(chiave) for flag, chiave in MAPPA_FILTRI_AMENITA)
                    and (not cancellazione_obbligatoria or dest.get("cancellazione_gratuita"))
                    and (categoria_minima == 0 or (dest.get("categoria_stelle") or 0) >= categoria_minima)
                    and (not valutazione_minima or (dest.get("valutazione") or 0.0) >= valutazione_minima)
                )
            # Distanza da un punto di riferimento manuale: km/tempo di guida REALI (OSRM, già
            # precalcolati in batch sopra), economica perché è solo una lookup in un dizionario.
            if rispetta_filtri and punto_riferimento_coord is not None:
                diagnostica_distanze["punto_valutati"] += 1
                info_punto = mappa_distanza_punto.get((dest["lat"], dest["lon"]))
                if info_punto is None:
                    diagnostica_distanze["punto_sconosciuti"] += 1
                    rispetta_filtri = False
                else:
                    entro_soglia = (
                        info_punto["km"] <= distanza_punto_max_km if unita_punto == "Km reali in auto"
                        else (info_punto["ore"] * 60) <= tempo_punto_max_min
                    )
                    if not entro_soglia:
                        diagnostica_distanze["punto_lontani"] += 1
                    rispetta_filtri = entro_soglia
            # Distanza da mare/lago: la più costosa (Overpass + OSRM per ogni hotel, seppur
            # cache-ate), per questo va per ultima — si valuta solo su chi ha già superato il resto.
            if rispetta_filtri and vicino_mare_attivo:
                diagnostica_distanze["mare_valutati"] += 1
                info_mare = distanza_acqua_auto(round(dest["lat"], 4), round(dest["lon"], 4), "mare")
                if info_mare is None:
                    diagnostica_distanze["mare_sconosciuti"] += 1
                    rispetta_filtri = False
                else:
                    entro_soglia = (
                        info_mare["km"] <= distanza_mare_max_km if unita_mare == "Km reali in auto"
                        else (info_mare["ore"] * 60) <= tempo_mare_max_min
                    )
                    if not entro_soglia:
                        diagnostica_distanze["mare_lontani"] += 1
                    rispetta_filtri = entro_soglia
            if rispetta_filtri and vicino_lago_attivo:
                diagnostica_distanze["lago_valutati"] += 1
                info_lago = distanza_acqua_auto(round(dest["lat"], 4), round(dest["lon"], 4), "lago")
                if info_lago is None:
                    diagnostica_distanze["lago_sconosciuti"] += 1
                    rispetta_filtri = False
                else:
                    entro_soglia = (
                        info_lago["km"] <= distanza_lago_max_km if unita_lago == "Km reali in auto"
                        else (info_lago["ore"] * 60) <= tempo_lago_max_min
                    )
                    if not entro_soglia:
                        diagnostica_distanze["lago_lontani"] += 1
                    rispetta_filtri = entro_soglia
            if rispetta_filtri:
                dest = dict(dest)  # non modificare l'originale tra un rerun e l'altro

                if raggiungibile_auto:
                    km_autostrada_ar = dest["distanza_km"] * 2 * (quota_km_autostrada / 100)
                    costo_benzina_chf = round((dest["distanza_km"] / 100) * consumo_medio * 2 * costo_benzina, 2)
                    costo_pedaggio_eur = km_autostrada_ar * tariffa_pedaggio_eur_km
                    costo_pedaggio_chf = round(costo_pedaggio_eur * cambio_eur_chf, 2)
                    costo_vignetta_chf = 0.0 if ha_gia_vignetta else VIGNETTA_SVIZZERA_CHF
                    costo_viaggio_chf = round(costo_benzina_chf + costo_pedaggio_chf + costo_vignetta_chf, 2)
                else:
                    # Nessun percorso stradale: benzina/pedaggio/vignetta non hanno senso (servirebbe
                    # un traghetto o un aereo, con costi di tutt'altra natura che non stimiamo qui).
                    costo_benzina_chf = costo_pedaggio_chf = costo_vignetta_chf = costo_viaggio_chf = None

                dest["costo_hotel_chf"] = dest["prezzo_notte_chf"] * giorni_soggiorno
                dest["costo_benzina_chf"] = costo_benzina_chf
                dest["costo_pedaggio_chf"] = costo_pedaggio_chf
                dest["costo_vignetta_chf"] = costo_vignetta_chf
                dest["costo_viaggio_chf"] = costo_viaggio_chf
                dest["budget_globale_chf"] = dest["costo_hotel_chf"] + (costo_viaggio_chf or 0.0)
                dest["servizi"] = icone_servizi(dest)
                dest["categoria"] = testo_categoria(dest)
                dest["valutazione_testo"] = testo_valutazione(dest)
                dest["periodo"] = f"{data_checkin.strftime('%d.%m.%Y')} → {data_checkout.strftime('%d.%m.%Y')}"
                dest["link_maps"] = f"https://www.google.com/maps/search/?api=1&query={dest['lat']},{dest['lon']}"
                risultati.append(dest)

        df = pd.DataFrame(risultati)


        # Diagnostica: spiega esplicitamente quanti hotel sono stati esclusi dai filtri di
        # distanza mare/lago/punto e PERCHÉ (troppo lontani vs. distanza non determinabile),
        # invece di lasciare che "Nessuna struttura trovata" resti un messaggio senza spiegazione.
        messaggi_diagnostica = []
        if ore_guida_oltre_limite:
            minimo = min(ore_guida_oltre_limite)
            messaggi_diagnostica.append(
                f"🚗 Limite ore in auto: {len(ore_guida_oltre_limite)} hotel avevano un percorso "
                f"stradale REALE trovato da OSRM (il più vicino richiedeva {minimo:.1f} ore), ma oltre "
                f"il limite di {ore_auto_max} ore impostato nel passo '2️⃣'. Se una di queste mete "
                "richiede in realtà un traghetto, ma OpenStreetMap include una linea traghetto "
                "percorribile nei suoi dati stradali, OSRM la considera comunque 'raggiungibile in "
                "auto' (seppur con un tempo enorme) — in questo caso il checkbox 'Includi mete non "
                "raggiungibili' non si attiva, perché un percorso esiste davvero. Alza il limite di "
                "ore per includerla comunque."
            )
        if esclusi_non_raggiungibili and not includi_non_raggiungibili:
            messaggi_diagnostica.append(
                f"⛴️ {esclusi_non_raggiungibili} hotel esclusi perché OSRM non ha trovato alcun "
                "percorso stradale (probabile meta raggiungibile solo via traghetto/aereo). Attiva "
                "'Includi anche mete non raggiungibili in auto' nel passo '2️⃣' per includerli."
            )
        if vicino_mare_attivo and diagnostica_distanze["mare_valutati"] > 0:
            d = diagnostica_distanze
            messaggi_diagnostica.append(
                f"🌊 Filtro 'Vicino al mare': su {d['mare_valutati']} hotel esaminati, "
                f"{d['mare_lontani']} erano oltre la soglia impostata e per {d['mare_sconosciuti']} non è "
                "stato possibile determinare la distanza dalla costa (nessun tratto di costa trovato entro "
                "20 km in linea d'aria, oppure OpenStreetMap/OSRM non ha risposto)."
            )
        if vicino_lago_attivo and diagnostica_distanze["lago_valutati"] > 0:
            d = diagnostica_distanze
            messaggi_diagnostica.append(
                f"🏞️ Filtro 'Vicino a un lago': su {d['lago_valutati']} hotel esaminati, "
                f"{d['lago_lontani']} erano oltre la soglia impostata e per {d['lago_sconosciuti']} non è "
                "stato possibile determinare la distanza dal lago (nessun lago trovato entro 20 km in linea "
                "d'aria, oppure OpenStreetMap/OSRM non ha risposto)."
            )
        if punto_riferimento_coord is not None and diagnostica_distanze["punto_valutati"] > 0:
            d = diagnostica_distanze
            messaggi_diagnostica.append(
                f"📍 Filtro punto di riferimento: su {d['punto_valutati']} hotel esaminati, "
                f"{d['punto_lontani']} erano oltre la soglia impostata e per {d['punto_sconosciuti']} non è "
                "stato possibile calcolare il percorso (OSRM non ha risposto)."
            )
        if messaggi_diagnostica:
            with st.expander("🔍 Perché alcuni hotel sono stati esclusi dai filtri di distanza", expanded=df.empty):
                for msg in messaggi_diagnostica:
                    st.caption(msg)
                errori_overpass = st.session_state.get("errori_overpass")
                if errori_overpass:
                    st.error(
                        "Errori di rete REALI rilevati verso OpenStreetMap/Overpass durante questa "
                        "ricerca (probabile causa dei casi 'non è stato possibile determinare la "
                        "distanza' sopra):\n\n" + "\n".join(f"- {e}" for e in errori_overpass)
                    )


        # --- INTERFACCIA E ANALISI ---
        if not df.empty:
            st.subheader("🎯 Risultati trovati")
            colonne_mostrate = ["nome", "regione", "categoria_ricerca", "categoria", "valutazione_testo", "servizi", "ore_guida", "prezzo_notte_chf", "link_maps", "fonte_prezzo", "costo_benzina_chf", "costo_pedaggio_chf", "costo_vignetta_chf", "budget_globale_chf"]
            formati = {
                "ore_guida": lambda x: f"{x:.1f} h" if x is not None else "⛴️ Traghetto/aereo",
                "prezzo_notte_chf": lambda x: swiss_chf(x),
                "costo_benzina_chf": lambda x: swiss_chf(x) if x is not None else "n/d",
                "costo_pedaggio_chf": lambda x: swiss_chf(x) if x is not None else "n/d",
                "costo_vignetta_chf": lambda x: swiss_chf(x) if x is not None else "n/d",
                "budget_globale_chf": lambda x: swiss_chf(x),
            }
            st.dataframe(
                df[colonne_mostrate].style.format(formati),
                use_container_width=True,
                column_config={
                    "link_maps": st.column_config.LinkColumn(
                        "Google Maps", display_text="🗺️ Apri in Maps",
                        help="Apre la posizione della struttura in una nuova scheda su Google Maps",
                    ),
                },
            )

            if usa_ricerca_geografica and sorgente_dati is not destinazioni_mock:
                metodo_distanza = df["fonte_distanza"].iloc[0] if "fonte_distanza" in df.columns and not df.empty else ""
                st.caption(
                    f"Ricerca da **{st.session_state.get('ricerca_geo_origine', origen)}** il "
                    f"{st.session_state['ricerca_geo_timestamp'].strftime('%d.%m.%Y alle %H:%M')}, per il "
                    f"{data_checkin.strftime('%d.%m.%Y')} → {data_checkout.strftime('%d.%m.%Y')} "
                    f"({adulti} adulti, {bambini} bambini). Km/ore di guida calcolati con: {metodo_distanza}. "
                    "Premi di nuovo 'Cerca hotel nelle zone' nella sidebar per cercare ancora."
                )
            else:
                st.caption("👀 Stai vedendo un'anteprima con dati di esempio statici (fittizi).")
                if st.button("✖️ Nascondi l'anteprima di esempio"):
                    st.session_state["mostra_demo"] = False
                    st.rerun()
            st.caption(
                "Il pedaggio è una stima basata sulla tariffa media Classe A e sulla quota di percorso "
                "in autostrada impostate nella sidebar; la vignetta svizzera (CHF 40, se non già posseduta) "
                "è conteggiata una sola volta a prescindere dal numero di viaggi nell'anno."
            )

            st.subheader("🗺️ Posizioni")
            st.map(df[['lat', 'lon']])

            # Esportazione (funzione to_excel definita a inizio file, con colonne dimensionate,
            # testo a capo e palette di colori tenue)
            st.download_button("📊 Scarica Excel", data=to_excel(df), file_name="vacanze.xlsx")
        else:
            st.warning("Nessuna struttura trovata con questi parametri.")
