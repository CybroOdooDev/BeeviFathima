"""Who has punches on a ZKTeco terminal: User ID, name, count, first/last punch.
Read-only — nothing on the device is changed."""
import sys
from collections import defaultdict
from zk import ZK

host = sys.argv[1] if len(sys.argv) > 1 else "10.0.11.43"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 4370

conn = ZK(host, port=port, timeout=15, ommit_ping=True).connect()
try:
    names = {u.user_id: u.name for u in conn.get_users()}
    punches = defaultdict(list)
    for a in conn.get_attendance():
        punches[a.user_id].append(a.timestamp)
finally:
    conn.disconnect()

print(f"{'User ID':<12} {'Name':<24} {'Punches':>7}  {'First punch':<16}  Last punch")
for user_id in sorted(punches, key=lambda u: (len(u), u)):
    times = sorted(punches[user_id])
    print(f"{user_id:<12} {names.get(user_id, '(not in user list)'):<24} {len(times):>7}  "
          f"{times[0]:%Y-%m-%d %H:%M}  {times[-1]:%Y-%m-%d %H:%M}")
print(f"\n{len(punches)} of {len(names)} enrolled user(s) have punches on this device.")