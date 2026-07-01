import streamlit as st
import pandas as pd
import io
import requests
from datetime import date, timedelta
import plotly.express as px

# Configurazione della pagina
st.set_page_config(page_title="Smart Holiday Finder", layout="wide")

# --- RICERCA PREZZI IN TEMPO REALE (SerpApi → Google Hotels) ---
# Google Hotels aggrega e mostra i prezzi già pubblicati da Booking.com, Expedia,
# hotel diretti, ecc. — quindi le eventuali commissioni di queste piattaforme sono
# già incluse nel prezzo che l'ospite pagherebbe, esattamente come richiesto.
@st.cache_data(ttl=3600, show_spinner=False)
def cerca_prezzo_reale(query, check_in_str, check_out_str, adulti, bambini, api_key):
    """Interroga SerpApi (engine=google_hotels) e restituisce (prezzo_per_notte_chf, fonte) o (None, motivo)."""
    try:
        params = {
            "engine": "google_hotels",
            "q": query,
            "check_in_date": check_in_str,
            "check_out_date": check_out_str,
            "adults": int(adulti),
            "children": int(bambini),
            "currency": "CHF",
            "hl": "it",
            "gl": "ch",
            "api_key": api_key,
        }
        r = requests.get("https://serpapi.com/search", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()

        if "error" in data:
            return None, f"Errore API: {data['error']}"

        proprieta = data.get("properties", [])
        if not proprieta:
            return None, "Nessun risultato trovato per questa struttura"

        prezzo = proprieta[0].get("rate_per_night", {}).get("extracted_lowest")
        if prezzo is None:
            return None, "Prezzo non disponibile per le date scelte"
        return float(prezzo), "🟢 Google Hotels (tempo reale)"
    except requests.exceptions.RequestException as e:
        return None, f"Errore di connessione: {e}"
    except Exception as e:
        return None, f"Errore imprevisto: {e}"

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
        "ℹ️ **Nota sui dati**: le strutture e le coordinate sono un database di esempio scritto nel "
        "codice (`destinazioni_mock`) — la lista di hotel resta fissa. I **prezzi per notte**, invece, "
        "possono essere aggiornati in tempo reale tramite Google Hotels (sezione '🔎 Prezzi in tempo "
        "reale' nella sidebar): in quel caso variano davvero in base alla stagione, alle date scelte e "
        "alla disponibilità. Senza attivare questa opzione, i prezzi restano quelli statici di esempio.",
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

    st.sidebar.markdown("**🔎 Prezzi in tempo reale**")
    st.sidebar.caption(
        "Interroga Google Hotels tramite [SerpApi](https://serpapi.com/manage-api-key) (registrazione "
        "gratuita, 250 ricerche/mese incluse). I prezzi restituiti sono quelli mostrati da Booking.com, "
        "Expedia e affini: eventuali commissioni delle piattaforme sono già comprese nell'importo."
    )
    usa_prezzi_reali = st.sidebar.checkbox("Attiva prezzi in tempo reale", value=False)
    serpapi_key = ""
    data_checkin = date.today() + timedelta(days=30)
    if usa_prezzi_reali:
        serpapi_key = st.sidebar.text_input("SerpApi API Key", type="password")
        data_checkin = st.sidebar.date_input("Data check-in", value=data_checkin, min_value=date.today())
        avvia_ricerca = st.sidebar.button("🔍 Cerca prezzi ora", disabled=not serpapi_key)
        if avvia_ricerca:
            st.session_state["prezzi_reali_attivi"] = True
        if not serpapi_key:
            st.sidebar.warning("Inserisci la tua API key per attivare la ricerca.")
    prezzi_reali_attivi = usa_prezzi_reali and serpapi_key and st.session_state.get("prezzi_reali_attivi", False)

    st.sidebar.markdown("**🚗 Costi di Viaggio (A/R):**")
    costo_benzina = st.sidebar.number_input("Costo carburante (CHF/litro)", value=1.80, step=0.05)
    consumo_medio = st.sidebar.number_input("Consumo medio (litri/100km)", value=7.0, step=0.5)

    st.sidebar.markdown("**🛣️ Pedaggi autostradali**")
    st.sidebar.caption(
        "Le mete proposte si raggiungono percorrendo autostrade italiane a sistema chiuso (pedaggio "
        "proporzionale ai km). Non essendoci un motore di instradamento collegato, il costo per casello "
        "esatto non è calcolabile con precisione: usiamo la tariffa media ufficiale per veicoli di "
        "Classe A (auto, aggiornata al 1° gennaio 2026, IVA inclusa), applicata ai km autostradali "
        "stimati della tratta. Puoi correggere la tariffa se conosci il percorso esatto."
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

    # --- DATABASE SIMULATO ---
    destinazioni_mock = [
        {"nome": "Hotel Savoy Palace (4★) - Riva del Garda", "regione": "Trentino (Lago)", "ore_guida": 3.5, "distanza_km": 250, "lat": 45.8893, "lon": 10.8431, "piscina": True, "prezzo_notte_chf": 620, "descrizione": "Resort con grandi piscine.", "tipo": "Lago / Relax"},
        {"nome": "Albergo Deva (3★) - Riva del Garda", "regione": "Trentino (Lago)", "ore_guida": 3.5, "distanza_km": 250, "lat": 45.9100, "lon": 10.8200, "piscina": True, "prezzo_notte_chf": 280, "descrizione": "Hotel con vista.", "tipo": "Lago / Economica"},
        {"nome": "Hotel Careni Villa Italia (3★) - Finale Ligure", "regione": "Liguria (Mare)", "ore_guida": 3.2, "distanza_km": 278, "lat": 44.1741, "lon": 8.3537, "piscina": True, "prezzo_notte_chf": 187, "descrizione": "Elegante edificio anni 30.", "tipo": "Mare / Spiaggia"},
        {"nome": "Relais Santa Chiara (4★) - San Gimignano", "regione": "Toscana (Campagna)", "ore_guida": 4.8, "distanza_km": 436, "lat": 43.4672, "lon": 11.0434, "piscina": True, "prezzo_notte_chf": 254, "descrizione": "Immerso nel verde.", "tipo": "Campagna / Relax"},
        {"nome": "Camping Il Boschetto di Piemma (3★) - San Gimignano", "regione": "Toscana (Campagna)", "ore_guida": 4.8, "distanza_km": 436, "lat": 43.4550, "lon": 11.0500, "piscina": True, "prezzo_notte_chf": 123, "descrizione": "Soluzione natura.", "tipo": "Campagna / Economica"}
    ]

    # --- LOGICA FILTRAGGIO ---
    risultati = []
    data_checkout = data_checkin + timedelta(days=giorni_soggiorno)

    if prezzi_reali_attivi:
        spinner_ctx = st.spinner("Ricerca prezzi in tempo reale su Google Hotels...")
    else:
        spinner_ctx = None

    def _filtra_destinazioni():
        for dest in destinazioni_mock:
            if dest["ore_guida"] <= ore_auto_max and (not piscina_obbligatoria or dest["piscina"]):
                dest = dict(dest)  # non modificare il dizionario originale tra un rerun e l'altro

                if prezzi_reali_attivi:
                    prezzo_reale, fonte = cerca_prezzo_reale(
                        f"{dest['nome']}", data_checkin.isoformat(), data_checkout.isoformat(),
                        adulti, bambini, serpapi_key,
                    )
                    if prezzo_reale is not None:
                        dest["prezzo_notte_chf"] = prezzo_reale
                        dest["fonte_prezzo"] = fonte
                    else:
                        dest["fonte_prezzo"] = f"⚪ Stima statica ({fonte})"
                else:
                    dest["fonte_prezzo"] = "⚪ Stima statica (prezzi in tempo reale disattivati)"

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

    if spinner_ctx:
        with spinner_ctx:
            _filtra_destinazioni()
    else:
        _filtra_destinazioni()

    df = pd.DataFrame(risultati)

    # --- INTERFACCIA E ANALISI ---
    if not df.empty:
        st.subheader("🎯 Risultati trovati")
        colonne_mostrate = ["nome", "regione", "prezzo_notte_chf", "fonte_prezzo", "costo_benzina_chf", "costo_pedaggio_chf", "costo_vignetta_chf", "budget_globale_chf"]
        formati = {
            "prezzo_notte_chf": lambda x: swiss_chf(x),
            "costo_benzina_chf": lambda x: swiss_chf(x),
            "costo_pedaggio_chf": lambda x: swiss_chf(x),
            "costo_vignetta_chf": lambda x: swiss_chf(x),
            "budget_globale_chf": lambda x: swiss_chf(x),
        }
        st.dataframe(df[colonne_mostrate].style.format(formati), use_container_width=True)
        if prezzi_reali_attivi:
            st.caption(
                f"Prezzi verificati su Google Hotels per il {data_checkin.strftime('%d.%m.%Y')} → "
                f"{data_checkout.strftime('%d.%m.%Y')} ({adulti} adulti, {bambini} bambini). Le strutture "
                "con esito '⚪ Stima statica' non hanno restituito un risultato (nome non trovato, "
                "nessuna disponibilità per quelle date, o quota SerpApi esaurita) e mantengono il prezzo "
                "di esempio del database."
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
