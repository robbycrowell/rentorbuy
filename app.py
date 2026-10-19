import os
from flask import Flask, jsonify, request, render_template_string
import requests

app = Flask(__name__)

FRED_KEY = os.environ.get("FRED_API_KEY", "")
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

REGION_RENT = {
    "Northeast": "CUUR0100SEHA",
    "Midwest": "CUUR0200SEHA",
    "South": "CUUR0300SEHA",
    "West": "CUUR0400SEHA",
}

STATE_REGION = {
    "CT": "Northeast", "ME": "Northeast", "MA": "Northeast", "NH": "Northeast", "RI": "Northeast",
    "VT": "Northeast", "NJ": "Northeast", "NY": "Northeast", "PA": "Northeast",
    "IL": "Midwest", "IN": "Midwest", "MI": "Midwest", "OH": "Midwest", "WI": "Midwest",
    "IA": "Midwest", "KS": "Midwest", "MN": "Midwest", "MO": "Midwest", "NE": "Midwest",
    "ND": "Midwest", "SD": "Midwest",
    "DE": "South", "DC": "South", "FL": "South", "GA": "South", "MD": "South", "NC": "South",
    "SC": "South", "VA": "South", "WV": "South", "AL": "South", "KY": "South", "MS": "South",
    "TN": "South", "AR": "South", "LA": "South", "OK": "South", "TX": "South",
    "AZ": "West", "CO": "West", "ID": "West", "MT": "West", "NV": "West", "NM": "West",
    "UT": "West", "WY": "West", "AK": "West", "CA": "West", "HI": "West", "OR": "West", "WA": "West",
}

# Assumptions (shown to the user in the Model Transparency box)
FRONT_END_DTI = 0.28
BACK_END_DTI = 0.36
OWNERSHIP_COST_RATE = 0.015   # property tax + insurance + maintenance, % of home value per year
CLOSING_COST_RATE = 0.03
SELLING_COST_RATE = 0.06
RENT_TO_PRICE_MONTHLY = 0.005  # starting rent = 0.5% of home price per month
MIN_DOWN_PCT = 0.03
TERM_MONTHS = 360


def fetch_series(series_id, limit):
    """Return list of (date, float) newest first, skipping missing values."""
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY is not set")
    r = requests.get(
        FRED_URL,
        params={
            "series_id": series_id,
            "api_key": FRED_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"FRED returned {r.status_code} for {series_id}")
    obs = r.json().get("observations", [])
    out = []
    for o in obs:
        try:
            out.append((o["date"], float(o["value"])))
        except (ValueError, KeyError):
            continue
    if not out:
        raise RuntimeError(f"No usable data for {series_id}")
    return out


def pmt_factor(annual_rate, months=TERM_MONTHS):
    r = annual_rate / 12
    if r == 0:
        return 1 / months
    return r / (1 - (1 + r) ** -months)


def balance_after(loan, annual_rate, months_paid):
    r = annual_rate / 12
    pay = loan * pmt_factor(annual_rate)
    if r == 0:
        return max(0.0, loan - pay * months_paid)
    return max(0.0, loan * (1 + r) ** months_paid - pay * (((1 + r) ** months_paid - 1) / r))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def analyze(inputs):
    income = inputs["income"]
    debt = inputs["debt"]
    savings = inputs["savings"]
    years = inputs["years"]
    state = inputs["state"]

    region = STATE_REGION[state]
    hpi_id = f"{state}STHPI"
    us_hpi_id = "USSTHPI"
    rent_id = REGION_RENT[region]
    price_id = "MSPUS"
    rate_id = "MORTGAGE30US"

    rate_obs = fetch_series(rate_id, 1)
    hpi_obs = fetch_series(hpi_id, 21)       # ~5 years of quarterly data
    us_hpi_obs = fetch_series(us_hpi_id, 1)
    price_obs = fetch_series(price_id, 1)
    rent_obs = fetch_series(rent_id, 13)     # 12 months of change

    mortgage_rate = rate_obs[0][1] / 100
    state_hpi = hpi_obs[0][1]
    us_hpi = us_hpi_obs[0][1]
    us_median_price = price_obs[0][1]

    # KPI 1: state-adjusted home price
    home_price = us_median_price * (state_hpi / us_hpi)

    # Appreciation: annualized change over the available window (up to 5 years), clamped
    n_q = min(20, len(hpi_obs) - 1)
    raw_apprec = (hpi_obs[0][1] / hpi_obs[n_q][1]) ** (4 / n_q) - 1 if n_q > 0 else 0.0
    appreciation = clamp(raw_apprec, 0.0, 0.06)

    # Rent growth: 12-month change in regional rent CPI, clamped
    raw_rent_g = rent_obs[0][1] / rent_obs[-1][1] - 1 if len(rent_obs) > 1 else 0.0
    rent_growth = clamp(raw_rent_g, 0.0, 0.08)

    # KPI 2: max and comfortable mortgage
    max_pay = max(0.0, BACK_END_DTI * income - debt)
    comfy_pay = max(0.0, min(FRONT_END_DTI * income, BACK_END_DTI * income - debt))
    f = pmt_factor(mortgage_rate)
    max_loan = max_pay / f
    comfy_loan = comfy_pay / f

    down = min(savings, home_price)
    loan = max(0.0, home_price - down)
    down_pct = down / home_price if home_price else 0.0
    monthly_pi = loan * f
    monthly_own = home_price * OWNERSHIP_COST_RATE / 12
    monthly_buy = monthly_pi + monthly_own
    monthly_rent = home_price * RENT_TO_PRICE_MONTHLY

    # Year-by-year cumulative comparison (net cost of buying vs cumulative rent)
    upfront = down + home_price * CLOSING_COST_RATE
    rows = []
    breakeven = None
    cum_rent = 0.0
    cum_own = 0.0
    for t in range(1, 31):
        rent_t = monthly_rent * (1 + rent_growth) ** (t - 1) * 12
        cum_rent += rent_t
        value_prev = home_price * (1 + appreciation) ** (t - 1)
        cum_own += monthly_pi * 12 + value_prev * OWNERSHIP_COST_RATE
        value_t = home_price * (1 + appreciation) ** t
        equity = value_t * (1 - SELLING_COST_RATE) - balance_after(loan, mortgage_rate, 12 * t)
        net_buy = upfront + cum_own - equity
        rows.append({"year": t, "rent": round(cum_rent), "buy": round(net_buy)})
        if breakeven is None and net_buy < cum_rent:
            breakeven = t

    # Guardrails and recommendation
    warnings = []
    qualifies = loan <= max_loan
    if down_pct < MIN_DOWN_PCT:
        recommendation = "RENT"
        reason_core = (f"your savings cover only {down_pct*100:.1f}% of the estimated {state} home price, "
                       f"below the {MIN_DOWN_PCT*100:.0f}% minimum down payment the model requires")
        warnings.append("Savings are below the minimum down payment, so Rent is forced regardless of other metrics.")
    elif not qualifies:
        recommendation = "RENT"
        reason_core = (f"the loan you would need (${loan:,.0f}) is above your maximum affordable mortgage "
                       f"(${max_loan:,.0f}) at a 36% debt-to-income limit")
        warnings.append("Required loan exceeds the maximum affordable mortgage.")
    elif breakeven is not None and breakeven <= years:
        recommendation = "BUY"
        reason_core = (f"buying breaks even in year {breakeven}, inside your {years}-year timeline, "
                       f"and the required loan (${loan:,.0f}) fits under your max mortgage (${max_loan:,.0f})")
    else:
        recommendation = "RENT"
        be_txt = f"year {breakeven}" if breakeven else "after 30 years"
        reason_core = (f"buying only breaks even in {be_txt}, which is past your {years}-year timeline")

    if loan > comfy_loan and recommendation == "BUY":
        warnings.append("Buying is affordable at the 36% limit but above the comfortable (28% housing) level.")
    if raw_apprec != appreciation:
        warnings.append(f"Home appreciation was capped from {raw_apprec*100:.1f}% to {appreciation*100:.1f}% per year for projections.")
    if raw_rent_g != rent_growth:
        warnings.append(f"Rent growth was capped from {raw_rent_g*100:.1f}% to {rent_growth*100:.1f}% per year for projections.")
    if years < 3:
        warnings.append("Horizons under 3 years almost always favor renting because of closing and selling costs.")

    series = [
        {"role": "Mortgage rate (national)", "id": rate_id, "date": rate_obs[0][0], "value": f"{rate_obs[0][1]:.2f}%"},
        {"role": f"{state} home price index", "id": hpi_id, "date": hpi_obs[0][0], "value": f"{state_hpi:,.2f}"},
        {"role": "US home price index", "id": us_hpi_id, "date": us_hpi_obs[0][0], "value": f"{us_hpi:,.2f}"},
        {"role": "US median sales price", "id": price_id, "date": price_obs[0][0], "value": f"${us_median_price:,.0f}"},
        {"role": f"Rent CPI ({region} region)", "id": rent_id, "date": rent_obs[0][0], "value": f"{rent_obs[0][1]:,.3f}"},
    ]

    return {
        "recommendation": recommendation,
        "why": f"Recommendation is {recommendation.title()} because {reason_core}.",
        "kpis": {
            "breakeven_year": breakeven,
            "max_mortgage": round(max_loan),
            "comfortable_mortgage": round(comfy_loan),
            "est_home_price": round(home_price),
            "down_payment_pct": round(down_pct * 100, 1),
            "monthly_buy": round(monthly_buy),
            "monthly_rent": round(monthly_rent),
            "mortgage_rate": round(mortgage_rate * 100, 2),
            "appreciation": round(appreciation * 100, 2),
            "rent_growth": round(rent_growth * 100, 2),
        },
        "region": region,
        "series": series,
        "rows": rows[: max(years, 10)],
        "warnings": warnings,
    }


def parse_inputs(data):
    errs = []

    def num(name, lo, hi, label):
        try:
            v = float(data.get(name))
        except (TypeError, ValueError):
            errs.append(f"{label} must be a number.")
            return 0.0
        if v < lo or v > hi:
            errs.append(f"{label} must be between {lo:,.0f} and {hi:,.0f}.")
        return v

    income = num("income", 1, 5_000_000, "Monthly gross income")
    debt = num("debt", 0, 5_000_000, "Monthly debt")
    savings = num("savings", 0, 50_000_000, "Savings")
    years = num("years", 1, 30, "Years before moving")
    state = str(data.get("state", "")).upper()
    if state not in STATE_REGION:
        errs.append("Pick a valid state.")
    if not errs and debt >= income:
        errs.append("Monthly debt can't be equal to or higher than monthly income.")
    return errs, {"income": income, "debt": debt, "savings": savings, "years": int(years), "state": state}


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    errs, inputs = parse_inputs(request.get_json(silent=True) or {})
    if errs:
        return jsonify({"error": " ".join(errs)}), 400
    try:
        return jsonify(analyze(inputs))
    except Exception as e:  # fallback: never show made-up numbers if live data fails
        return jsonify({"error": f"Live FRED data could not be loaded ({e}). No recommendation was produced."}), 502


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RentOrBuy</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,Arial,sans-serif;margin:0;background:#f4f5f7;color:#1a1a1a}
.wrap{max-width:1000px;margin:0 auto;padding:24px 16px}
h1{margin:0 0 4px;font-size:26px}.sub{color:#555;margin:0 0 20px}
.grid{display:grid;grid-template-columns:320px 1fr;gap:16px}
@media(max-width:800px){.grid{grid-template-columns:1fr}}
.card{background:#fff;border:1px solid #dcdfe4;border-radius:10px;padding:16px;margin-bottom:16px}
label{display:block;font-size:13px;font-weight:600;margin:12px 0 4px}
input,select{width:100%;padding:9px;border:1px solid #bbb;border-radius:6px;font-size:14px}
button{margin-top:16px;width:100%;padding:11px;border:0;border-radius:6px;background:#1d4ed8;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.kpi{background:#f8f9fb;border:1px solid #e1e4e8;border-radius:8px;padding:10px}
.kpi .l{font-size:12px;color:#666}.kpi .v{font-size:20px;font-weight:700;margin-top:2px}
.rec{border-radius:10px;padding:14px;margin-bottom:12px}
.rec.buy{background:#ecfdf3;border:1.5px solid #16a34a}.rec.rent{background:#eff6ff;border:1.5px solid #2563eb}
.rec h2{margin:0 0 6px;font-size:22px}
.warn{background:#fffbeb;border:1px solid #f59e0b;border-radius:8px;padding:8px 12px;font-size:13px;margin-top:10px}
.err{background:#fef2f2;border:1px solid #dc2626;border-radius:8px;padding:10px;color:#991b1b;font-size:14px}
.trans{background:#fff7ed;border:1.5px solid #ea580c;border-radius:10px;padding:12px 16px}
.trans h3{margin:0 0 6px;font-size:15px}.trans ul{margin:0;padding-left:18px;font-size:13px;line-height:1.5}
table{width:100%;border-collapse:collapse;font-size:12.5px}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid #eee}
.muted{color:#666;font-size:12px}
</style></head><body><div class="wrap">
<h1>RentOrBuy</h1>
<p class="sub">A FRED-powered rent, buy, and affordability advisor for first-time homebuyers.</p>
<div class="grid">
<div class="card">
<b>Your inputs</b>
<label>Monthly gross income ($)</label><input id="income" type="number" value="12000">
<label>Existing monthly debt ($)</label><input id="debt" type="number" value="600">
<label>Savings for down payment ($)</label><input id="savings" type="number" value="150000">
<label>Expected years before moving</label><input id="years" type="number" value="8" min="1" max="30">
<label>State</label><select id="state"></select>
<button onclick="run()">Get recommendation</button>
<p class="muted">Pulls live data from FRED when you click.</p>
</div>
<div id="out"><div class="card muted">Enter your inputs and click Get recommendation.</div></div>
</div></div>
<script>
const STATES="AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split(" ");
const sel=document.getElementById("state");
STATES.forEach(s=>{const o=document.createElement("option");o.value=s;o.textContent=s;if(s==="CA")o.selected=true;sel.appendChild(o)});
const $=n=>"$"+Number(n).toLocaleString();
async function run(){
  const out=document.getElementById("out");
  out.innerHTML='<div class="card muted">Loading live FRED data...</div>';
  const body={income:income.value,debt:debt.value,savings:savings.value,years:years.value,state:state.value};
  let d;
  try{const r=await fetch("/api/analyze",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});d=await r.json();}
  catch(e){out.innerHTML='<div class="err">Network error. Try again.</div>';return;}
  if(d.error){out.innerHTML='<div class="err">'+d.error+'</div>';return;}
  const k=d.kpis,cls=d.recommendation==="BUY"?"buy":"rent";
  out.innerHTML=`
  <div class="card">
    <div class="rec ${cls}"><h2>Recommendation: ${d.recommendation}</h2><div>${d.why}</div></div>
    <div class="kpis">
      <div class="kpi"><div class="l">Breakeven year</div><div class="v">${k.breakeven_year?("Year "+k.breakeven_year):"30+ yrs"}</div></div>
      <div class="kpi"><div class="l">Max mortgage (36% DTI)</div><div class="v">${$(k.max_mortgage)}</div></div>
      <div class="kpi"><div class="l">Comfortable mortgage (28%)</div><div class="v">${$(k.comfortable_mortgage)}</div></div>
      <div class="kpi"><div class="l">Est. ${state.value} home price</div><div class="v">${$(k.est_home_price)}</div></div>
      <div class="kpi"><div class="l">Down payment</div><div class="v">${k.down_payment_pct}%</div></div>
      <div class="kpi"><div class="l">Monthly: buy vs rent</div><div class="v">${$(k.monthly_buy)} / ${$(k.monthly_rent)}</div></div>
    </div>
    ${d.warnings.map(w=>'<div class="warn">Warning: '+w+'</div>').join("")}
  </div>
  <div class="card"><b>Why this recommendation?</b>
    <p style="font-size:14px;line-height:1.5">${d.why} Over ${years.value} years the model compares cumulative rent (starting near ${$(k.monthly_rent)}/mo, growing ${k.rent_growth}%/yr with ${d.region} rent CPI) against the net cost of buying (down payment, closing costs, mortgage at ${k.mortgage_rate}%, ownership costs, minus equity after selling costs, with ${k.appreciation}%/yr home appreciation from the ${state.value} price index).</p>
  </div>
  <div class="card"><b>Live data used (from FRED)</b>
    <table><tr><th>Role</th><th>Series ID</th><th>Latest obs.</th><th>Value</th></tr>
    ${d.series.map(s=>`<tr><td>${s.role}</td><td><a href="https://fred.stlouisfed.org/series/${s.id}" target="_blank">${s.id}</a></td><td>${s.date}</td><td>${s.value}</td></tr>`).join("")}</table>
    <p class="muted">Home price is state-specific (state HPI). Rent trend is regional (${d.region} Census region), not state-specific.</p>
  </div>
  <div class="trans"><h3>Model transparency</h3><ul>
    <li>Assumes a 30-year fixed mortgage at the national average rate; your actual rate depends on credit, lender, and loan type.</li>
    <li>Home price is estimated as US median price scaled by your state's HPI; starting rent is assumed at 0.5% of home price per month and grows with regional rent CPI.</li>
    <li>Includes 1.5%/yr ownership costs, 3% closing, and 6% selling costs; it ignores taxes by bracket, investment returns on your down payment, and your full finances.</li>
    <li>This is a general estimate, not financial advice. State HPI lags by about a quarter.</li>
  </ul></div>`;
}
</script></body></html>"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
