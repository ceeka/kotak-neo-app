import os
import io
import json
import time
import requests
import pandas as pd
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from neo_api_client import NeoAPI

app = Flask(__name__, static_folder='.')
CORS(app)

current_session = None
scrip_master_df = None

# --- SERVE FRONTEND ---
@app.route('/')
def root():
    return send_from_directory('.', 'index.html')

# --- UTILS ---
def extract_cash(limits_data):
    try:
        if not limits_data: return "0.00"
        data = limits_data.get('data', limits_data) if isinstance(limits_data, dict) else limits_data
        if isinstance(data, list) and data: data = data[0]
        if not isinstance(data, dict): return "0.00"
        for k in ['Net', 'net', 'cash', 'Cash', 'available_balance']:
            if k in data and data[k]: return str(data[k])
        return "0.00"
    except: return "0.00"

# --- API ROUTES ---
@app.route('/api/login', methods=['POST'])
def login():
    global current_session, scrip_master_df
    data = request.json
    try:
        client = NeoAPI(consumer_key=data['consumer_key'], environment='prod')
        client.totp_login(mobile_number=data['mobile_number'], ucc=data['client_code'], totp=data['totp'])
        client.totp_validate(mpin=data['mpin'])
        current_session = client
        
        # Non-blocking master download attempt
        try:
            print("⏳ Downloading Scrip Master...")
            resp = client.scrip_master()
            fno_url = next((url for url in resp.get('filesPaths', []) if 'nse_fo' in url), None)
            if fno_url:
                s_resp = requests.get(fno_url)
                scrip_master_df = pd.read_csv(io.StringIO(s_resp.text))
                scrip_master_df.columns = scrip_master_df.columns.str.strip()
                print(f"✅ Master Loaded: {len(scrip_master_df)} rows")
        except: print("⚠️ Master Download Skipped")
        
        funds = extract_cash(client.limits())
        return jsonify({"success": True, "user": {"name": data['client_code'], "funds": funds}})
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 401

@app.route('/api/search', methods=['GET'])
def search_scrip():
    global scrip_master_df
    query = request.args.get('q', '').upper()
    if scrip_master_df is None or len(query) < 3: return jsonify([])
    try:
        mask = scrip_master_df['pTrdSymbol'].str.contains(query, na=False)
        results = scrip_master_df[mask].head(10)
        col = 'pSymbol' if 'pSymbol' in results.columns else results.columns[0]
        return jsonify(results[['pTrdSymbol', 'lLotSize', col]].rename(columns={col: 'token'}).to_dict(orient='records'))
    except: return jsonify([])

@app.route('/api/get_ltp', methods=['GET'])
def get_ltp():
    if not current_session: return jsonify({"success": False, "ltp": 0})
    try:
        token = request.args.get('token')
        q_resp = current_session.quotes(instrument_tokens=[{'instrument_token': token, 'exchange_segment': 'nse_fo'}], quote_type='ltp')
        ltp = float(q_resp['data'][0]['last_price']) if q_resp and 'data' in q_resp else 0
        return jsonify({"success": True, "ltp": ltp})
    except: return jsonify({"success": False, "ltp": 0})

@app.route('/api/place_order', methods=['POST'])
def place_order():
    if not current_session: return jsonify({"error": "Login first"}), 401
    d = request.json
    try:
        raw_side = str(d.get('side', '')).upper()
        clean_side = "B" if "B" in raw_side else "S"
        raw_price = str(d.get('price', '')).strip() or "0"
        
        # Slicing Logic
        total_qty = int(d.get('qty', 0))
        slice_size = int(d.get('slice_size', 0))
        
        base_params = {
            "exchange_segment": d.get('segment', 'nse_fo'),
            "product": d.get('product', 'NRML'),
            "price": raw_price,
            "order_type": "MKT" if d.get('is_market') else "L",
            "trading_symbol": d.get('symbol'),
            "transaction_type": clean_side,
            "validity": "DAY"
        }

        orders_placed = []
        if slice_size <= 0 or slice_size >= total_qty:
            base_params['quantity'] = str(total_qty)
            resp = current_session.place_order(**base_params)
            orders_placed.append(resp.to_dict() if hasattr(resp, 'to_dict') else resp)
        else:
            remaining = total_qty
            while remaining > 0:
                current_qty = min(remaining, slice_size)
                base_params['quantity'] = str(current_qty)
                resp = current_session.place_order(**base_params)
                r_dict = resp.to_dict() if hasattr(resp, 'to_dict') else resp
                orders_placed.append(r_dict)
                remaining -= current_qty
                time.sleep(0.2)

        return jsonify({"success": True, "data": orders_placed, "is_sliced": len(orders_placed) > 1})
    except Exception as e: return jsonify({"success": False, "error": str(e)})

@app.route('/api/data', methods=['GET'])
def get_data():
    if not current_session: return jsonify({"error": "Login first"}), 401
    try:
        raw_pos = current_session.positions().get('data', [])
        funds = extract_cash(current_session.limits())
        
        # LTP Patch
        open_tokens = []
        for p in raw_pos:
            if float(p.get('flBuyQty',0)) != float(p.get('flSellQty',0)):
                open_tokens.append({'instrument_token': str(p.get('tok')), 'exchange_segment': 'nse_fo'})
        
        ltp_map = {}
        if open_tokens:
            try:
                q_resp = current_session.quotes(instrument_tokens=open_tokens, quote_type='ltp')
                if q_resp and 'data' in q_resp:
                    for i in q_resp['data']: ltp_map[str(i['instrument_token'])] = float(i['last_price'])
            except: pass

        for p in raw_pos: p['fetchedLTP'] = ltp_map.get(str(p.get('tok')), 0)

        return jsonify({"success": True, "positions": raw_pos, "orders": current_session.order_report(), "funds": funds})
    except Exception as e: return jsonify({"success": False, "error": str(e)})

@app.route('/api/logout', methods=['POST'])
def logout():
    global current_session; current_session = None; return jsonify({"success": True})

if __name__ == '__main__':
    # Cloud Run Logic: Use PORT env variable
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)