import streamlit as st
import pandas as pd
import io
import math
import requests
from datetime import date, timedelta, datetime
import plotly.express as px

# Configurazione della pagina
st.set_page_config(page_title="Smart Holiday Finder", layout="wide")

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

def geocodifica_citta(nome_citta):
    """Nominatim (OpenStreetMap): nome città -> (lat, lon). Gratuito, nessuna chiave."""
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


def calcola_distanze_reali(origine_lat, origine_lon, destinazioni):
    """
    destinazioni: lista di (lat, lon).
    Prova OSRM (routing reale, un'unica chiamata batch via 'table service') e in caso di
    errore ricade su una stima in linea d'aria corretta con un fattore di curvatura stradale.
    Ritorna una lista di dict: {"km": float, "ore": float, "metodo": str}.
    """
    if not destinazioni:
        return []

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
                    risultati.append(None)
                else:
                    risultati.append({"km": dist / 1000, "ore": d / 3600, "metodo": "OSRM (instradamento reale)"})
            # se OSRM ha risposto Ok ma con dei "None" isolati, li copriamo col fallback puntuale
            for i, (lat, lon) in enumerate(destinazioni):
                if risultati[i] is None:
                    km_lin = _haversine_km(origine_lat, origine_lon, lat, lon)
                    risultati[i] = {"km": km_lin * 1.55, "ore": (km_lin * 1.55) / 65, "metodo": "Stima lineare (fallback)"}
            return risultati
    except Exception:
        pass

    # Fallback completo: stima in linea d'aria per tutte le destinazioni
    risultati = []
    for lat, lon in destinazioni:
        km_lin = _haversine_km(origine_lat, origine_lon, lat, lon)
        km_stimati = km_lin * 1.55  # fattore di curvatura stradale (percorsi alpini/collinari)
        risultati.append({"km": km_stimati, "ore": km_stimati / 65, "metodo": "Stima lineare (fallback)"})
    return risultati


def cerca_hotel_zona(zona_query, check_in_str, check_out_str, adulti, bambini, api_key, max_risultati=10):
    """SerpApi → Google Hotels: cerca hotel per zona/città. Ritorna (lista_proprieta, errore)."""
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
        r = requests.get("https://serpapi.com/search", params=params, timeout=25)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            return [], f"Errore API: {data['error']}"
        return data.get("properties", [])[:max_risultati], None
    except requests.exceptions.RequestException as e:
        return [], f"Errore di connessione: {e}"
    except Exception as e:
        return [], f"Errore imprevisto: {e}"


def _ha_piscina(amenities):
    """Cerca 'pool'/'piscina' (IT/EN, come restituiti da Google Hotels) nella lista amenities."""
    if not amenities:
        return False
    return any("pool" in a.lower() or "piscina" in a.lower() for a in amenities)


def esegui_ricerca_geografica(origine_lat, origine_lon, zone, check_in, check_out, adulti, bambini, api_key):
    """
    Orchestratore: per ogni zona interroga Google Hotels (SerpApi), poi calcola in un'unica
    chiamata OSRM per zona le distanze/durate reali di tutti gli hotel trovati in quella zona.
    Ritorna (lista_hotel_trovati, lista_avvisi).
    """
    hotel_trovati = []
    avvisi = []

    for zona in zone:
        proprieta, errore = cerca_hotel_zona(zona, check_in.isoformat(), check_out.isoformat(), adulti, bambini, api_key)
        if errore:
            avvisi.append(f"Zona '{zona}': {errore}")
            continue
        if not proprieta:
            avvisi.append(f"Zona '{zona}': nessun hotel trovato da Google Hotels")
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
            avvisi.append(f"Zona '{zona}': risultati trovati ma senza coordinate o prezzo utilizzabili")
            continue

        distanze = calcola_distanze_reali(origine_lat, origine_lon, coordinate)

        for p, dist in zip(proprieta_valide, distanze):
            gps = p["gps_coordinates"]
            hotel_trovati.append({
                "nome": p.get("name", "Struttura senza nome"),
                "regione": zona,
                "lat": gps["latitude"],
                "lon": gps["longitude"],
                "piscina": _ha_piscina(p.get("amenities")),
                "prezzo_notte_chf": float(p["rate_per_night"]["extracted_lowest"]),
                "distanza_km": round(dist["km"], 1),
                "ore_guida": round(dist["ore"], 1),
                "fonte_prezzo": "🟢 Google Hotels (tempo reale)",
                "fonte_distanza": dist["metodo"],
                "descrizione": p.get("description", ""),
                "tipo": p.get("type", "hotel"),
            })

    return hotel_trovati, avvisi


# --- FORMATTAZIONE SVIZZERA (apostrofo come separatore delle migliaia) ---
def swiss_num(x, decimals=0):
    """Formatta un numero con l'apostrofo come separatore delle migliaia (stile svizzero)."""
    s = f"{x:,.{decimals}f}"
    return s.replace(",", "'")

def swiss_chf(x, decimals=0):
    return f"CHF {swiss_num(x, decimals)}"


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
if check_password():
    st.title("🧳 Smart Holiday Finder con AI")
    st.subheader("Filtra le mete, calcola budget completi e visualizza i risultati")

    st.info(
        "ℹ️ **Nota sui dati**: finché non avvii una ricerca geografica (sezione '🌍 Ricerca geografica "
        "in tempo reale' nella sidebar), vedi un piccolo database di esempio scritto nel codice, con "
        "prezzi e distanze statici. Dopo aver premuto '🔍 Cerca hotel nelle zone', la lista viene "
        "sostituita con hotel realmente trovati da Google Hotels nelle zone che indichi, con prezzi, "
        "km ed ore di guida reali calcolati da un motore di instradamento.",
        icon="ℹ️",
    )

    # --- SIDEBAR: CRITERI DI RICERCA ---
    st.sidebar.header("1. Imposta i tuoi Filtri")
    origen = st.sidebar.text_input("Città di partenza:", value="Lugano")

    st.sidebar.markdown("**Membri del viaggio:**")
    adulti = st.sidebar.number_input("Adulti", min_value=1, max_value=10, value=3)
    bambini = st.sidebar.number_input("Bambini", min_value=0, max_value=10, value=1)

    st.sidebar.markdown("**Limiti di viaggio:**")
    ore_auto_max = st.sidebar.slider("Tempo massimo in auto (ore):", min_value=1.0, max_value=12.0, value=5.0, step=0.5)
    piscina_obbligatoria = st.sidebar.checkbox("Richiedi Piscina", value=True)
    giorni_soggiorno = st.sidebar.slider("Durata soggiorno (giorni):", min_value=4, max_value=14, value=7)

    st.sidebar.markdown("**🌍 Ricerca geografica in tempo reale**")
    st.sidebar.caption(
        "Cerca hotel realmente disponibili in una o più zone tramite Google Hotels ([SerpApi]"
        "(https://serpapi.com/manage-api-key), 250 ricerche/mese gratuite), calcolando km e ore di "
        "guida reali da OSRM (motore di instradamento gratuito). I prezzi mostrati includono le "
        "eventuali commissioni di Booking/Expedia/ecc., perché sono quelli reali pubblicati online."
    )
    usa_ricerca_geografica = st.sidebar.checkbox("Attiva ricerca geografica", value=False)
    serpapi_key = ""
    data_checkin = date.today() + timedelta(days=30)
    zone_testo = "hotel Riva del Garda\nhotel Finale Ligure\nhotel San Gimignano"
    avvia_ricerca = False

    if usa_ricerca_geografica:
        serpapi_key = st.sidebar.text_input("SerpApi API Key", type="password")
        data_checkin = st.sidebar.date_input("Data check-in", value=data_checkin, min_value=date.today())
        zone_testo = st.sidebar.text_area(
            "Zone da esplorare (una per riga):", value=zone_testo, height=100,
            help="Ogni riga è una ricerca separata su Google Hotels (es. 'hotel Verona', 'resort Costa "
                 "Azzurra', 'agriturismo Chianti'). Più righe aggiungi, più ricerche vengono consumate."
        )
        avvia_ricerca = st.sidebar.button("🔍 Cerca hotel nelle zone", disabled=not serpapi_key)
        if not serpapi_key:
            st.sidebar.warning("Inserisci la tua API key per attivare la ricerca.")

        ultima_ricerca = st.session_state.get("ricerca_geo_timestamp")
        if ultima_ricerca:
            n_hotel = len(st.session_state.get("hotel_trovati", []))
            st.sidebar.caption(f"🕒 Ultima ricerca: {ultima_ricerca.strftime('%d.%m.%Y alle %H:%M')} — {n_hotel} hotel trovati")
        else:
            st.sidebar.caption("Nessuna ricerca ancora effettuata: vedi il database di esempio finché non premi il pulsante.")

        st.sidebar.caption(
            "⚠️ Cambiare gli altri filtri (ore auto, piscina, ecc.) **non** avvia una nuova ricerca: "
            "filtra semplicemente gli hotel già trovati. Premi di nuovo il pulsante per cercarne altri."
        )

    st.sidebar.markdown("**🚗 Costi di Viaggio (A/R):**")
    costo_benzina = st.sidebar.number_input("Costo carburante (CHF/litro)", value=1.80, step=0.05)
    consumo_medio = st.sidebar.number_input("Consumo medio (litri/100km)", value=7.0, step=0.5)

    st.sidebar.markdown("**🛣️ Pedaggi autostradali**")
    st.sidebar.caption(
        "Non essendoci un calcolo casello-per-casello disponibile, usiamo la tariffa media ufficiale "
        "per veicoli di Classe A (auto, aggiornata al 1° gennaio 2026, IVA inclusa) applicata ai km "
        "reali del percorso (ora calcolati da OSRM quando la ricerca geografica è attiva)."
    )
    tariffa_pedaggio_eur_km = st.sidebar.slider(
        "Tariffa media pedaggio (EUR/km, Classe A):",
        min_value=0.05, max_value=0.15, value=0.085, step=0.005,
        help="Le tratte di pianura costano in media 0,07-0,08 EUR/km, quelle di montagna "
             "(gallerie, viadotti, es. Brennero) fino a 0,10-0,13 EUR/km."
    )
    quota_km_autostrada = st.sidebar.slider(
        "% del percorso realmente in autostrada a pedaggio:",
        min_value=50, max_value=100, value=90, step=5,
        help="Gli ultimi km per raggiungere l'hotel spesso sono su strade extraurbane non a pedaggio."
    )
    cambio_eur_chf = st.sidebar.number_input(
        "Cambio EUR → CHF (CHF per 1 EUR):", value=0.93, step=0.01,
        help="Verifica il cambio attuale: variazioni anche piccole incidono sul costo del pedaggio."
    )

    st.sidebar.markdown("**🇨🇭 Vignetta autostradale svizzera**")
    ha_gia_vignetta = st.sidebar.checkbox(
        "Possiedo già la vignetta svizzera valida", value=False,
        help="La vignetta annuale svizzera costa CHF 40.- (obbligatoria per percorrere anche solo un "
             "breve tratto di autostrada/semi-autostrada svizzera, es. Lugano-Chiasso)."
    )
    VIGNETTA_SVIZZERA_CHF = 40.0

    # --- DATABASE DI ESEMPIO (usato solo finché non si esegue una ricerca geografica) ---
    destinazioni_mock = [
        {"nome": "Hotel Savoy Palace (4★) - Riva del Garda", "regione": "Trentino (Lago)", "ore_guida": 3.5, "distanza_km": 250, "lat": 45.8893, "lon": 10.8431, "piscina": True, "prezzo_notte_chf": 620, "descrizione": "Resort con grandi piscine.", "tipo": "Lago / Relax"},
        {"nome": "Albergo Deva (3★) - Riva del Garda", "regione": "Trentino (Lago)", "ore_guida": 3.5, "distanza_km": 250, "lat": 45.9100, "lon": 10.8200, "piscina": True, "prezzo_notte_chf": 280, "descrizione": "Hotel con vista.", "tipo": "Lago / Economica"},
        {"nome": "Hotel Careni Villa Italia (3★) - Finale Ligure", "regione": "Liguria (Mare)", "ore_guida": 3.2, "distanza_km": 278, "lat": 44.1741, "lon": 8.3537, "piscina": True, "prezzo_notte_chf": 187, "descrizione": "Elegante edificio anni 30.", "tipo": "Mare / Spiaggia"},
        {"nome": "Relais Santa Chiara (4★) - San Gimignano", "regione": "Toscana (Campagna)", "ore_guida": 4.8, "distanza_km": 436, "lat": 43.4672, "lon": 11.0434, "piscina": True, "prezzo_notte_chf": 254, "descrizione": "Immerso nel verde.", "tipo": "Campagna / Relax"},
        {"nome": "Camping Il Boschetto di Piemma (3★) - San Gimignano", "regione": "Toscana (Campagna)", "ore_guida": 4.8, "distanza_km": 436, "lat": 43.4550, "lon": 11.0500, "piscina": True, "prezzo_notte_chf": 123, "descrizione": "Soluzione natura.", "tipo": "Campagna / Economica"}
    ]
    for d in destinazioni_mock:
        d["fonte_prezzo"] = "⚪ Stima statica (dati di esempio)"
        d["fonte_distanza"] = "Stima statica (dati di esempio)"

    # --- ESECUZIONE RICERCA GEOGRAFICA (solo al click del pulsante) ---
    data_checkout = data_checkin + timedelta(days=giorni_soggiorno)

    if usa_ricerca_geografica and avvia_ricerca and serpapi_key:
        with st.spinner("Geocodifica della città di partenza..."):
            origine_coord = geocodifica_citta(origen)

        if origine_coord is None:
            st.sidebar.error(f"Non sono riuscito a geolocalizzare '{origen}'. Controlla il nome della città.")
        else:
            zone = [z.strip() for z in zone_testo.splitlines() if z.strip()]
            with st.spinner(f"Ricerca hotel in {len(zone)} zone su Google Hotels e calcolo distanze reali..."):
                hotel_trovati, avvisi = esegui_ricerca_geografica(
                    origine_coord[0], origine_coord[1], zone,
                    data_checkin, data_checkout, adulti, bambini, serpapi_key,
                )
            st.session_state["hotel_trovati"] = hotel_trovati
            st.session_state["ricerca_geo_timestamp"] = datetime.now()
            st.session_state["ricerca_geo_avvisi"] = avvisi
            st.session_state["ricerca_geo_origine"] = origen

    # --- SORGENTE DATI: hotel trovati realmente, se presenti, altrimenti il database di esempio ---
    sorgente_dati = st.session_state.get("hotel_trovati") if usa_ricerca_geografica else None
    if sorgente_dati is None:
        sorgente_dati = destinazioni_mock

    if usa_ricerca_geografica and st.session_state.get("ricerca_geo_avvisi"):
        with st.expander("⚠️ Avvisi dell'ultima ricerca geografica"):
            for avviso in st.session_state["ricerca_geo_avvisi"]:
                st.caption(avviso)

    # --- LOGICA FILTRAGGIO (legge solo dalla sorgente dati già disponibile, mai nuove chiamate) ---
    risultati = []
    for dest in sorgente_dati:
        if dest["ore_guida"] <= ore_auto_max and (not piscina_obbligatoria or dest["piscina"]):
            dest = dict(dest)  # non modificare l'originale tra un rerun e l'altro

            km_autostrada_ar = dest["distanza_km"] * 2 * (quota_km_autostrada / 100)

            costo_benzina_chf = round((dest["distanza_km"] / 100) * consumo_medio * 2 * costo_benzina, 2)
            costo_pedaggio_eur = km_autostrada_ar * tariffa_pedaggio_eur_km
            costo_pedaggio_chf = round(costo_pedaggio_eur * cambio_eur_chf, 2)
            costo_vignetta_chf = 0.0 if ha_gia_vignetta else VIGNETTA_SVIZZERA_CHF

            dest["costo_hotel_chf"] = dest["prezzo_notte_chf"] * giorni_soggiorno
            dest["costo_benzina_chf"] = costo_benzina_chf
            dest["costo_pedaggio_chf"] = costo_pedaggio_chf
            dest["costo_vignetta_chf"] = costo_vignetta_chf
            dest["costo_viaggio_chf"] = round(costo_benzina_chf + costo_pedaggio_chf + costo_vignetta_chf, 2)
            dest["budget_globale_chf"] = dest["costo_hotel_chf"] + dest["costo_viaggio_chf"]
            risultati.append(dest)

    df = pd.DataFrame(risultati)

    # --- INTERFACCIA E ANALISI ---
    if not df.empty:
        st.subheader("🎯 Risultati trovati")
        colonne_mostrate = ["nome", "regione", "ore_guida", "prezzo_notte_chf", "fonte_prezzo", "costo_benzina_chf", "costo_pedaggio_chf", "costo_vignetta_chf", "budget_globale_chf"]
        formati = {
            "ore_guida": lambda x: f"{x:.1f} h",
            "prezzo_notte_chf": lambda x: swiss_chf(x),
            "costo_benzina_chf": lambda x: swiss_chf(x),
            "costo_pedaggio_chf": lambda x: swiss_chf(x),
            "costo_vignetta_chf": lambda x: swiss_chf(x),
            "budget_globale_chf": lambda x: swiss_chf(x),
        }
        st.dataframe(df[colonne_mostrate].style.format(formati), use_container_width=True)

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
            st.caption(
                "Stai vedendo il database di esempio statico. Attiva la ricerca geografica nella "
                "sidebar per trovare hotel reali."
            )
        st.caption(
            "Il pedaggio è una stima basata sulla tariffa media Classe A e sulla quota di percorso "
            "in autostrada impostate nella sidebar; la vignetta svizzera (CHF 40, se non già posseduta) "
            "è conteggiata una sola volta a prescindere dal numero di viaggi nell'anno."
        )

        st.subheader("🗺️ Posizioni")
        st.map(df[['lat', 'lon']])

        # Esportazione
        def to_excel(df):
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                df.to_excel(writer, index=False, sheet_name="Vacanze")
                worksheet = writer.sheets["Vacanze"]

                # Excel usa il separatore delle migliaia legato al locale di sistema, quindi per
                # garantire l'apostrofo indipendentemente dal locale dell'utente scriviamo i valori
                # monetari già formattati come testo (CHF 1'234).
                colonne_chf = [
                    "prezzo_notte_chf", "costo_hotel_chf", "costo_benzina_chf",
                    "costo_pedaggio_chf", "costo_vignetta_chf", "costo_viaggio_chf",
                    "budget_globale_chf",
                ]
                for col in colonne_chf:
                    if col in df.columns:
                        col_idx = df.columns.get_loc(col)
                        for row_idx, val in enumerate(df[col], start=1):
                            worksheet.write_string(row_idx, col_idx, swiss_chf(val))
                        worksheet.set_column(col_idx, col_idx, max(14, len(col) + 2))

            return output.getvalue()

        st.download_button("📊 Scarica Excel", data=to_excel(df), file_name="vacanze.xlsx")
    else:
        st.warning("Nessuna struttura trovata con questi parametri.")
