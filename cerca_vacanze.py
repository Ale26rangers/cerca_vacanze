import streamlit as st
import pandas as pd
import io
import plotly.express as px

# Configurazione della pagina
st.set_page_config(page_title="Smart Holiday Finder", layout="wide")

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

    st.sidebar.markdown("**🚗 Costi di Viaggio (A/R):**")
    costo_benzina = st.sidebar.number_input("Costo carburante (CHF/litro)", value=1.80, step=0.05)
    consumo_medio = st.sidebar.number_input("Consumo medio (litri/100km)", value=7.0, step=0.5)
    pedaggi_stimati = st.sidebar.number_input("Pedaggi stimati (CHF)", value=50.0, step=5.0)

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
    for dest in destinazioni_mock:
        if dest["ore_guida"] <= ore_auto_max and (not piscina_obbligatoria or dest["piscina"]):
            dest["costo_hotel_chf"] = dest["prezzo_notte_chf"] * giorni_soggiorno
            dest["costo_viaggio_chf"] = round(((dest["distanza_km"] / 100) * consumo_medio * 2 * costo_benzina) + pedaggi_stimati, 2)
            dest["budget_globale_chf"] = dest["costo_hotel_chf"] + dest["costo_viaggio_chf"]
            risultati.append(dest)

    df = pd.DataFrame(risultati)

    # --- INTERFACCIA E ANALISI ---
    if not df.empty:
        st.subheader("🎯 Risultati trovati")
        st.dataframe(df[["nome", "regione", "budget_globale_chf"]].style.format({"budget_globale_chf": "CHF {:.0f}"}), use_container_width=True)
        
        st.subheader("🗺️ Posizioni")
        st.map(df[['lat', 'lon']])

        # Esportazione
        def to_excel(df):
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                df.to_excel(writer, index=False)
            return output.getvalue()

        st.download_button("📊 Scarica Excel", data=to_excel(df), file_name="vacanze.xlsx")
    else:
        st.warning("Nessuna struttura trovata con questi parametri.")