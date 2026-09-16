from flask import Flask, jsonify
from biotime import BiotimeClient
from odoo import OdooClient
import datetime


app = Flask(__name__)

biotime = BiotimeClient(base_url="http://biotimedxb.com:8007")

odoo = OdooClient(url="http://localhost:8019", api_key="552022097d43709547c47bfff9861e02569b28d0")

@app.route("/sync/attendance", methods=["GET","POST"])
def sync_attendance():
    now = datetime.datetime.now()
    # data = biotime.get_attendance()
    # for rec in data['data']:
    #     print(rec)
    employees = odoo.get_employees()
    for employee in employees:
        print('EMP::',employee.get('zk_last_sync'))
        data = biotime.get_attendance(employee.get('emp_code'), employee.get('zk_last_sync'))
        latest_sync_date = data.get('latest_sync_date')
        print('SUCCESS FULLY FETCHED ON: ',latest_sync_date, now.date())
        external_data(data.get('data'))

    return employees

def external_data(data):
    for rec in data:
        print('DATA::', rec)


# @app.route("/")
# def home():
#     return jsonify({
#         "status": "running..",
#         "service": "Biotime - Odoo Middelware"
#     })

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )