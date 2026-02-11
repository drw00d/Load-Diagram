# streamlit_app.py
import streamlit as st
import pandas as pd
import streamlit.components.v1 as components

st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

MASTER_PATH = "data/Ortec SP Product Master.xlsx"

# --- Product Master columns ---
COL_COMMODITY = "Product Type"
COL_FACILITY = "Facility Id"
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_EDGE = "Edge Type"            # for Machine Edge rule

COL_THICK = "Panel Thickness"
COL_WIDTH = "Width"
COL_LENGTH = "Length"

# --- Car rules ---
FLOOR_SPOTS = 15
DOOR_START_SPOT = 6
DOOR_END_SPOT = 9
DOORFRAME_SPOTS_NO_MACHINE_EDGE = {6, 9}
DOORWAY_SPOTS_ALLOW_MACHINE_EDGE = {7, 8}
AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]


# =============================
# Load + Normalize Product Master
# =============================
@st.cache_data(show_spinner=False)
def load_product_master(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    global COL_DESC
    if COL_DESC not in df.columns and "Descrip" in df.columns:
        COL_DESC = "Descrip"

    required = [COL_PRODUCT_ID, COL_UNIT_H, COL_UNIT_WT, COL_COMMODITY, COL_EDGE]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Product Master missing required columns: {missing}")

    df[COL_PRODUCT_ID] = df[COL_PRODUCT_ID].astype(str).str.strip()
    df
