import requests
import logging
import datetime

_logger = logging.getLogger(__name__)

class BiotimeClient:
    def __init__(self, base_url):
        self.base_url = base_url

    def get_attendance(self, emp_code, last_sync):
        # latest_sync_date = None
        data = []
        url = f"{self.base_url}/iclock/api/transactions/?start_time=2026-09-09&end_time=2026-09-14&emp_code={emp_code}"
        # start_time=2026-09-09&end_time=2026-09-14&
        while url:
            try:
                response = requests.get(url, headers={"Content-Type": "application/json", "Authorization": "Token fe5fe2ae008e670f3c66dca96f8311f4fe870f3c"}, timeout=30)
                response.raise_for_status()
                data += response.json()['data']
                if response.json()['next']:
                # print('PAGINATION....', response.json()['next'])
                    url = response.json()['next']
                    continue
                else:
                    break
                # latest_sync_date = datetime.datetime.now()
            except Exception as error:
                _logger.exception("ERROR OCCURED...%s", error)


        return {'data': data, 'latest_sync_date': datetime.datetime.now()}

    