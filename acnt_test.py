import demand as core, requests, json, os
from dotenv import load_dotenv; load_dotenv()

token = core.get_token(core.APP_KEY, core.APP_SECRET)
stex = os.getenv("KIWOOM_DMST_STEX_TP", "KRX")
headers = {
    "Content-Type": "application/json;charset=UTF-8",
    "authorization": f"Bearer {token}",
    "api-id": "ka10075",
}

for trde_tp, label in [("", "전체"), ("1", "매도"), ("2", "매수")]:
    for period in [("20260101","20260515"), ("20251001","20260515"), ("20250101","20260515")]:
        r = requests.post(f"{core.HOST}/api/dostk/acnt", headers=headers,
            json={"acnt_no":"6259247410", "strt_dt":period[0], "end_dt":period[1],
                  "stk_cd":"", "trde_tp":trde_tp, "all_stk_tp":"0", "stex_tp":stex},
            timeout=30)
        res = r.json()
        rows = res.get("oso") or []
        print(f"trde_tp={label} {period[0]}~{period[1]}: rows={len(rows)}")
        if rows:
            print("  첫번째:", json.dumps(rows[0], ensure_ascii=False)[:200])
            break
    print()