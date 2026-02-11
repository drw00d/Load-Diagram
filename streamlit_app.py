# streamlit_app.py
import streamlit as st
import pandas as pd
import streamlit.components.v1 as components

st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

MASTER_PATH = "data/Ortec SP Product Master.xlsx"

# --- expected columns in your master ---
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"          # or "Descrip"
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_HALF_PACK = "Half Pack"


@st.cache_data(show_spinner=False)
def load_product_master(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    # Keep only columns we care about (but don’t break if missing)
    keep = [c for c in [COL_PRODUCT_ID, COL_DESC, COL_ACTIVE, COL_UNIT_H, COL_UNIT_WT, COL_HALF_PACK] if c in df.columns]
    if keep:
        df = df[keep].copy()

    # Clean types
    df[COL_PRODUCT_ID] = df[COL_PRODUCT_ID].astype(str).str.strip()
    if COL_DESC in df.columns:
        df[COL_DESC] = df[COL_DESC].astype(str)

    if COL_UNIT_H in df.columns:
        df[COL_UNIT_H] = pd.to_numeric(df[COL_UNIT_H], errors="coerce")
    if COL_UNIT_WT in df.columns:
        df[COL_UNIT_WT] = pd.to_numeric(df[COL_UNIT_WT], errors="coerce")

    # Normalize Half Pack to bool-ish
    if COL_HALF_PACK in df.columns:
        hp = df[COL_HALF_PACK].astype(str).str.strip().str.upper()
        df[COL_HALF_PACK] = hp.isin(["Y", "YES", "TRUE", "1"])

    # Filter active if possible
    if COL_ACTIVE in df.columns:
        act = df[COL_ACTIVE].astype(str).str.strip().str.upper()
        df = df[act.isin(["Y", "YES", "TRUE", "1", "ACTIVE"])].copy()

    # Drop rows missing critical fields
    missing_cols = [c for c in [COL_PRODUCT_ID, COL_UNIT_H, COL_UNIT_WT] if c not in df.columns]_
