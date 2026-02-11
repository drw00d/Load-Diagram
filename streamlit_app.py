import streamlit as st

st.title("Load Diagram Optimizer")

st.write("If you can see this, deployment worked!")

car_id = st.text_input("Car ID", value="TBOX632012")
unit_height = st.number_input("Unit Height (in)", value=20.25)

if st.button("Run Test"):
    st.success(f"Car {car_id} processed.")
