import requests

class OdooClient:
    def __init__(self, url="http://localhost:8019", api_key="552022097d43709547c47bfff9861e02569b28d0"):
        self.url = url
        self.api_key = api_key

    def headers(self):
        return {
            "Authorization": f"bearer {self.api_key}"
        }

    def get_employees(self):
        url = f"{self.url}/json/2/hr.employee/search_read"
        params = {
            'domain': [
                ['is_zk_data','!=',False]
            ],
            'fields': ['name','emp_code','external_id','zk_last_sync', 'department_id'],
        }
        response = requests.post(url,headers=self.headers(), json=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def create_attendance(self, employee_id, check_in):
        url = f"{self.url}/json/2/hr.attendance/create"
        params = {
            'vals_list': [
                {
                    'employee_id': employee_id,
                    'check_in': check_in
                }
            ]
        }
        response = requests.post(url, headers=self.headers(),json=params,timeout=30)
        response.raise_for_status()
        return response.json()
