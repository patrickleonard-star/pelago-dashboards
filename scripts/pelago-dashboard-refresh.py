#!/usr/bin/env python3
"""
pelago-dashboard-refresh.py
----------------------------
Daily refresh for Pelago Growth/CS dashboards.
Queries Cube semantic layer → writes csm-data.json + yoy-data.json → commits to GitHub.

Schema contract: both data files follow schema_version "1.0".
HTML shell files (index.html, yoy-ir-el.html) are NEVER modified by this script.

Run: python3 pelago-dashboard-refresh.py
Requires env vars:
  CUBE_API_URL   - Cube Cloud REST API base URL
  CUBE_API_TOKEN - Cube API token
  GITHUB_TOKEN   - GitHub personal access token
  GITHUB_OWNER   - e.g. patrickleonard-star
  GITHUB_REPO    - e.g. pelago-dashboards
  GITHUB_BRANCH  - e.g. main
"""

import os, json, re, datetime, hashlib, base64, urllib.request, urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
CUBE_API_URL   = os.environ["CUBE_API_URL"]
CUBE_API_TOKEN = os.environ["CUBE_API_TOKEN"]
GITHUB_TOKEN   = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER   = os.environ.get("GITHUB_OWNER", "patrickleonard-star")
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "pelago-dashboards")
GITHUB_BRANCH  = os.environ.get("GITHUB_BRANCH", "main")

today = datetime.date.today()
CY    = today.year
PY    = CY - 1
YTD_MONTH = today.month  # 1-indexed

CY_MONTHS = [f"{m:02d}" for m in range(1, YTD_MONTH + 1)]  # ["01","02",...,"07"]
MON_LABELS_CY = [datetime.date(CY, int(m), 1).strftime("%b") + f"-{str(CY)[2:]}" for m in CY_MONTHS]
MON_LABELS_PY = [datetime.date(PY, int(m), 1).strftime("%b") + f"-{str(PY)[2:]}" for m in CY_MONTHS]

CY_START = f"{CY}-01-01"
CY_END   = f"{CY}-12-31"
PY_START = f"{PY}-01-01"
PY_END   = f"{PY}-12-31"

# ── Cube query helper ─────────────────────────────────────────────────────────
def cube_query(sql):
    """Execute a Cube SQL query; returns list of row lists matching schema order."""
    url = f"{CUBE_API_URL}/v1/sql"
    payload = json.dumps({"query": sql}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {CUBE_API_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data.get("data", [])

def cube_query_all(sql, page_size=5000):
    """Paginate through all results."""
    all_rows = []
    offset = 0
    base_sql = re.sub(r'\s+LIMIT\s+\d+\s*$', '', sql.strip(), flags=re.IGNORECASE)
    while True:
        paged_sql = f"{base_sql}\nLIMIT {page_size} OFFSET {offset}"
        rows = cube_query(paged_sql)
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return all_rows

# ── GitHub helpers ────────────────────────────────────────────────────────────
def github_get_sha(path):
    """Get current SHA of a file in the repo."""
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}"
    req = urllib.request.Request(url, headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

def github_put_file(path, content_bytes, message, sha=None):
    """Create or update a file in the repo."""
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode(),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json", "Accept": "application/vnd.github.v3+json"},
        method="PUT"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    return result["commit"]["sha"]

# ── Data pull functions ───────────────────────────────────────────────────────

def pull_billing(cy_start, cy_end, py_start, py_end):
    """Pull per-client billing totals for CY and PY."""
    cy_rows = cube_query_all(f"""
        SELECT
          billing_invoice.client_profile_client_name,
          billing_invoice.client_profile_sub_client_name,
          billing_invoice.client_profile_csm_name,
          MEASURE(billing_invoice.invoice_amount_sum) AS ir,
          MEASURE(billing_invoice.invoice_member_count) AS mem
        FROM billing_invoice
        WHERE billing_invoice.invoice_date >= '{cy_start}'
          AND billing_invoice.invoice_date <= '{cy_end}'
        GROUP BY 1,2,3
        ORDER BY 1,2
        LIMIT 5000
    """)
    py_rows = cube_query_all(f"""
        SELECT
          billing_invoice.client_profile_client_name,
          billing_invoice.client_profile_sub_client_name,
          MEASURE(billing_invoice.invoice_amount_sum) AS ir
        FROM billing_invoice
        WHERE billing_invoice.invoice_date >= '{py_start}'
          AND billing_invoice.invoice_date <= '{py_end}'
        GROUP BY 1,2
        ORDER BY 1,2
        LIMIT 5000
    """)
    return cy_rows, py_rows

def pull_renewal_billing_monthly(year_start, year_end, num_months):
    """Pull per-client monthly renewal billing."""
    rows = cube_query_all(f"""
        SELECT
          billing_invoice.client_profile_client_name,
          billing_invoice.client_profile_sub_client_name,
          DATE_TRUNC('month', billing_invoice.invoice_date) AS month,
          MEASURE(billing_invoice.invoice_amount_sum) AS ir
        FROM billing_invoice
        WHERE billing_invoice.invoice_date >= '{year_start}'
          AND billing_invoice.invoice_date <= '{year_end}'
          AND billing_invoice.billing_term = 'Renewal Billing Term'
        GROUP BY 1,2,3
        ORDER BY 1,2,3
        LIMIT 5000
    """)
    # Pivot to [cn, sc, m0, m1, ..., m(N-1)]
    from collections import defaultdict
    pivot = defaultdict(lambda: [0.0] * num_months)
    month_idx = {}
    for i in range(num_months):
        mo = datetime.date(int(year_start[:4]), i+1, 1).strftime("%Y-%m")
        month_idx[mo] = i
    for row in rows:
        cn, sc, mo_str, ir = row
        mo = str(mo_str)[:7]
        idx = month_idx.get(mo)
        if idx is not None:
            key = (cn or "", sc or "")
            pivot[key][idx] += float(ir or 0)
    return [[k[0], k[1] if k[1] else None] + v for k, v in pivot.items()]

def pull_billing_monthly(year_start, year_end, num_months):
    """Pull per-client monthly total billing."""
    rows = cube_query_all(f"""
        SELECT
          billing_invoice.client_profile_client_name,
          billing_invoice.client_profile_sub_client_name,
          DATE_TRUNC('month', billing_invoice.invoice_date) AS month,
          MEASURE(billing_invoice.invoice_amount_sum) AS ir
        FROM billing_invoice
        WHERE billing_invoice.invoice_date >= '{year_start}'
          AND billing_invoice.invoice_date <= '{year_end}'
        GROUP BY 1,2,3
        ORDER BY 1,2,3
        LIMIT 5000
    """)
    from collections import defaultdict
    pivot = defaultdict(lambda: [0.0] * num_months)
    month_idx = {}
    for i in range(num_months):
        mo = datetime.date(int(year_start[:4]), i+1, 1).strftime("%Y-%m")
        month_idx[mo] = i
    for row in rows:
        cn, sc, mo_str, ir = row
        mo = str(mo_str)[:7]
        idx = month_idx.get(mo)
        if idx is not None:
            key = (cn or "", sc or "")
            pivot[key][idx] += float(ir or 0)
    return [[k[0], k[1] if k[1] else None] + v for k, v in pivot.items()]

def pull_el_monthly(year, num_months):
    """Pull per-client monthly EL counts (single-month window)."""
    rows_all = []
    for mo in range(1, num_months + 1):
        mo_start = f"{year}-{mo:02d}-01"
        # Last day of month
        if mo == 12:
            mo_end = f"{year}-12-31"
        else:
            mo_end = (datetime.date(year, mo+1, 1) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        month_rows = cube_query_all(f"""
            SELECT
              eligibility_member_snapshots.client_profile_client_name,
              eligibility_member_snapshots.client_profile_sub_client_name,
              MEASURE(eligibility_member_snapshots.eligibility_member_count) AS el
            FROM eligibility_member_snapshots
            WHERE eligibility_member_snapshots.effective_start_date >= '{mo_start}'
              AND eligibility_member_snapshots.effective_start_date <= '{mo_end}'
            GROUP BY 1,2
            ORDER BY 1,2
            LIMIT 5000
        """)
        rows_all.append((mo - 1, month_rows))  # 0-indexed month
    # Pivot to [cn, sc, m0, ..., m(N-1)]
    from collections import defaultdict
    pivot = defaultdict(lambda: [0] * num_months)
    for mo_idx, month_rows in rows_all:
        for row in month_rows:
            cn, sc, el = row
            key = (cn or "", sc or "")
            pivot[key][mo_idx] = int(float(el or 0))
    return [[k[0], k[1] if k[1] else None] + v for k, v in pivot.items()]

def pull_growth(cy_start, cy_end):
    """Pull per-client growth outreach metrics."""
    rows = cube_query_all(f"""
        SELECT
          growth.client_profile_client_name,
          growth.client_profile_sub_client_name,
          MEASURE(growth.outreach_delivered_sum) AS tps,
          MEASURE(growth.time_based_registrations_all_outreach_count) AS regs,
          MEASURE(growth.outreach_unique_clicks_sum) AS unique_touched
        FROM growth
        WHERE growth.original_event_datetime >= '{cy_start}'
          AND growth.original_event_datetime <= '{cy_end}'
          AND growth.communication_type = 'Growth'
        GROUP BY 1,2
        ORDER BY 1,2
        LIMIT 5000
    """)
    return rows

def pull_meta(cy_start, cy_end):
    """Pull per-client metadata (cohort, product, go-live date)."""
    rows = cube_query_all(f"""
        SELECT DISTINCT
          billing_invoice.client_profile_client_name,
          billing_invoice.client_profile_sub_client_name,
          billing_invoice.client_profile_csm_name,
          billing_invoice.cohort,
          billing_invoice.product,
          billing_invoice.contract_start_date
        FROM billing_invoice
        WHERE billing_invoice.invoice_date >= '{cy_start}'
          AND billing_invoice.invoice_date <= '{cy_end}'
        ORDER BY 1,2
        LIMIT 5000
    """)
    return rows

def pull_close_won():
    """Pull close-won year per employer from account_opportunities."""
    rows = cube_query_all("""
        SELECT
          account_opportunities.employer_display_name,
          MAX(account_opportunities.close_date) AS latest_close
        FROM account_opportunities
        WHERE account_opportunities.is_won = 1
        GROUP BY 1
        ORDER BY 1
        LIMIT 5000
    """)
    result = {}
    for row in rows:
        employer, close_date = row
        if employer and close_date:
            result[str(employer)] = str(close_date)[:4]
    return result

# ── Monthly aggregate totals ──────────────────────────────────────────────────
def build_monthly_totals(cy_m, py_m, cy_el_m, py_el_m, num_months, cy, py):
    """Build monthly aggregate rows for the monthly view."""
    monthly_cy = []
    monthly_py = []
    for mo in range(num_months):
        cy_ir = sum(row[2 + mo] for row in cy_m if len(row) > 2 + mo)
        py_ir = sum(row[2 + mo] for row in py_m if len(row) > 2 + mo)
        cy_el = sum(row[2 + mo] for row in cy_el_m if len(row) > 2 + mo)
        py_el = sum(row[2 + mo] for row in py_el_m if len(row) > 2 + mo)
        label_cy = datetime.date(cy, mo+1, 1).strftime(f"%b-{str(cy)[2:]}")
        label_py = datetime.date(py, mo+1, 1).strftime(f"%b-{str(py)[2:]}")
        monthly_cy.append({"m": label_cy, "ir": round(cy_ir, 2), "ren": 0, "el": cy_el, "tp": 0, "reg": 0})
        monthly_py.append({"m": label_py, "ir": round(py_ir, 2), "ren": 0, "el": py_el, "tp": 0, "reg": 0})
    return monthly_cy, monthly_py

# ── Build ALL_ROWS for CSM dashboard ─────────────────────────────────────────
def build_all_rows(cy_rows, meta_rows, growth_rows, el_rows, close_won_dict, csm_map):
    """Build ALL_ROWS array for csm-data.json."""
    from collections import defaultdict

    def ck(cn, sc):
        return f"{cn or ''}||{(sc or '').strip()}"

    # Index meta
    meta_by_key = {}
    for row in meta_rows:
        cn, sc = row[0], row[1]
        meta_by_key[ck(cn, sc)] = row

    # Index EL
    el_by_key = {}
    for row in el_rows:
        cn, sc, el = row[0], row[1] if len(row) > 1 else None, row[2] if len(row) > 2 else 0
        el_by_key[ck(cn, sc)] = int(float(el or 0))

    # Index growth
    growth_by_name = {}
    for row in growth_rows:
        cn, sc = (row[0] or "").lower().strip(), (row[1] or "").lower().strip()
        growth_by_name[(cn, sc)] = {
            "tps": max(0, int(float(row[2] or 0))),
            "regs": int(float(row[3] or 0)),
            "unique_touched": max(0, int(float(row[4] or 0))),
        }

    all_rows = []
    for row in cy_rows:
        cn, sc, csm, ir, mem = row[0], row[1], row[2], float(row[3] or 0), int(float(row[4] or 0))
        k = ck(cn, sc)
        g = growth_by_name.get(((cn or "").lower().strip(), (sc or "").lower().strip()), {})
        avg_el = el_by_key.get(k, 0)
        emp = sc.strip() if sc and sc.strip() else cn
        cwy = close_won_dict.get(emp) or close_won_dict.get(cn) or None
        all_rows.append({
            "csm": csm or csm_map.get(k, "Unknown"),
            "ck": hashlib.md5(k.encode()).hexdigest(),
            "client_name": cn, "sub_client": sc,
            "display": f"{cn} - {sc}" if sc and sc.strip() else cn,
            "avg_els": avg_el,
            "regs": g.get("regs", 0),
            "tps": g.get("tps", 0),
            "unique_touched": g.get("unique_touched", 0),
            "y1": round(ir, 2), "rnwl": 0, "other": 0, "total": round(ir, 2),
            "is_unknown": False,
        })
    return all_rows

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{now}] Starting refresh: CY={CY}, PY={PY}, months={YTD_MONTH}")

    # Pull data
    print("Pulling billing CY/PY...")
    cy_rows, py_rows = pull_billing(CY_START, f"{CY}-{YTD_MONTH:02d}-31", PY_START, f"{PY}-{YTD_MONTH:02d}-31")

    print(f"Pulling monthly billing ({YTD_MONTH} months)...")
    cy_m = pull_billing_monthly(CY_START, f"{CY}-{YTD_MONTH:02d}-31", YTD_MONTH)
    py_m = pull_billing_monthly(PY_START, f"{PY}-{YTD_MONTH:02d}-31", YTD_MONTH)

    print("Pulling renewal billing...")
    cy_ren_m = pull_renewal_billing_monthly(CY_START, f"{CY}-{YTD_MONTH:02d}-31", YTD_MONTH)
    py_ren_m = pull_renewal_billing_monthly(PY_START, f"{PY}-{YTD_MONTH:02d}-31", YTD_MONTH)

    print(f"Pulling EL monthly ({YTD_MONTH} months × 2 years)...")
    cy_el_m = pull_el_monthly(CY, YTD_MONTH)
    py_el_m = pull_el_monthly(PY, YTD_MONTH)

    print("Pulling growth (TPS/regs/unique touched)...")
    growth_rows = pull_growth(CY_START, f"{CY}-{YTD_MONTH:02d}-31")

    print("Pulling metadata...")
    meta_rows = pull_meta(CY_START, f"{CY}-{YTD_MONTH:02d}-31")

    print("Pulling close-won years...")
    close_won = pull_close_won()

    # Build per-client EL averages (last month as snapshot)
    el_avg_cy = [[r[0], r[1], r[2 + YTD_MONTH - 1]] for r in cy_el_m if len(r) >= 2 + YTD_MONTH]
    el_avg_py = [[r[0], r[1], r[2 + YTD_MONTH - 1]] for r in py_el_m if len(r) >= 2 + YTD_MONTH]

    # PY EL parent (aggregate by client_name)
    from collections import defaultdict
    py_el_parent = defaultdict(int)
    for row in py_el_m:
        cn = row[0] or ""
        el = row[2 + YTD_MONTH - 1] if len(row) >= 2 + YTD_MONTH else 0
        py_el_parent[cn] = max(py_el_parent[cn], el)
    py_el_parent_dict = dict(py_el_parent)

    # Monthly totals
    monthly_cy, monthly_py = build_monthly_totals(cy_m, py_m, cy_el_m, py_el_m, YTD_MONTH, CY, PY)

    # ALL_ROWS for CSM dashboard
    all_rows = build_all_rows(cy_rows, meta_rows, growth_rows, el_avg_cy, close_won, {})

    # YOY_DATA
    yoy_by_key = {}
    from collections import defaultdict as dd
    def ck(cn, sc):
        return f"{cn or ''}||{(sc or '').strip()}"
    for row in cy_rows:
        k = ck(row[0], row[1])
        yoy_by_key.setdefault(k, {})["cy"] = row
    for row in py_rows:
        k = ck(row[0], row[1])
        yoy_by_key.setdefault(k, {})["py"] = row
    yoy_data_rows = []
    for k, v in yoy_by_key.items():
        cy_r = v.get("cy", [None, None, None, 0, 0])
        py_r = v.get("py", [None, None, 0])
        cn = cy_r[0] or py_r[0]
        sc = cy_r[1] if cy_r[1] is not None else py_r[1]
        cy_ir = float(cy_r[3] or 0)
        py_ir = float(py_r[2] or 0)
        cy_el_val = next((r[2+YTD_MONTH-1] for r in cy_el_m if r[0]==cn and (r[1] or "")==(sc or "")), 0)
        py_el_val = next((r[2+YTD_MONTH-1] for r in py_el_m if r[0]==cn and (r[1] or "")==(sc or "")), 0)
        emp = (sc or "").strip() or cn
        yoy_data_rows.append({
            "ck": hashlib.md5(k.encode()).hexdigest(),
            "csm": cy_r[2] if len(cy_r) > 2 else None,
            "client_name": cn, "sub_client": sc,
            "display": f"{cn} - {sc}" if sc and sc.strip() else cn,
            "close_won_year": int(close_won.get(emp) or close_won.get(cn) or 0) or None,
            "cy_el": cy_el_val, "py_el": py_el_val,
            "cy_ir": round(cy_ir, 2), "cy_ir_csm": round(cy_ir, 2),
            "py_ir": round(py_ir, 2),
            "cy_ir_per_el": round(cy_ir / cy_el_val, 4) if cy_el_val else 0,
            "py_ir_per_el": round(py_ir / py_el_val, 4) if py_el_val else 0,
            "d_el": cy_el_val - py_el_val,
            "d_ir": round(cy_ir - py_ir, 2),
            "d_ir_per_el": round((cy_ir/cy_el_val if cy_el_val else 0) - (py_ir/py_el_val if py_el_val else 0), 4),
            "is_unknown": False,
        })

    # CSMs list
    csm_set = sorted(set(r.get("csm") for r in all_rows if r.get("csm")))
    csms = [{"csm": c} for c in csm_set]

    # ── Assemble JSON files ────────────────────────────────────────────────
    csm_data = {
        "schema_version": "1.0",
        "generated_at": now,
        "as_of_date": str(today),
        "cy": CY, "py": PY, "ytd_month": YTD_MONTH,
        "csms": csms,
        "all_rows": all_rows,
        "yoy_data": yoy_data_rows,
    }

    yoy_data = {
        "schema_version": "1.0",
        "generated_at": now,
        "as_of_date": str(today),
        "cy": CY, "py": PY, "ytd_month": YTD_MONTH,
        "cy_mon_labels": MON_LABELS_CY,
        "py_mon_labels": MON_LABELS_PY,
        "monthly_cy": monthly_cy,
        "monthly_py": monthly_py,
        "raw_cy": [[r[0], r[1], r[2], float(r[3] or 0), int(float(r[4] or 0))] for r in cy_rows],
        "raw_py": [[r[0], r[1], float(r[2] or 0)] for r in py_rows],
        "raw_meta": [[r[0], r[1], r[2], r[3], r[4], str(r[5]) if r[5] else None] for r in meta_rows],
        "raw_el": el_avg_cy,
        "raw_py_el": el_avg_py,
        "raw_cy_el_m": cy_el_m,
        "raw_py_el_m": py_el_m,
        "raw_cy_m": cy_m,
        "raw_py_m": py_m,
        "raw_cy_ren_m": cy_ren_m,
        "raw_py_ren_m": py_ren_m,
        "close_won": close_won,
        "py_el_parent": py_el_parent_dict,
    }

    # ── Write to GitHub ────────────────────────────────────────────────────
    files = {
        "csm-data.json": json.dumps(csm_data, separators=(",", ":")),
        "yoy-data.json": json.dumps(yoy_data, separators=(",", ":")),
    }

    for path, content in files.items():
        print(f"Committing {path} ({len(content):,} chars)...")
        sha = github_get_sha(path)
        commit_sha = github_put_file(
            path,
            content.encode("utf-8"),
            f"data: refresh {path} for {today} ({YTD_MONTH} months)",
            sha=sha
        )
        print(f"  → commit {commit_sha}")

    print(f"[{datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}] Refresh complete.")

if __name__ == "__main__":
    main()
