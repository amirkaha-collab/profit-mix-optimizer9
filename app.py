"""
Profit Mix Optimizer – v7
=========================
Reads קרנות_השתלמות_חשיפות.xlsx and finds optimal 1/2/3-fund blends.

Root cause of all prior failures (Gemini + ChatGPT versions):
  - Wrong parameter row names were used ("יעד לחו״ל" etc. don't exist).
  - FX row ('חשיפה למט"ח') is row 7; code that read only 6 rows missed it.
  - Aggressive sheet-name filtering removed ALL valid sheets.

Actual Excel row names (verified):
  ROW_EQUITY   = 'סך חשיפה למניות מתוך כלל נכסי הקופה'
  ROW_ABROAD   = 'סך חשיפה לנכסים המושקעים בחו"ל מתוך כלל נכסי הקופה'
  ROW_SHARPE   = 'מדד שארפ'
  ROW_ILLIQUID = 'נכסים לא סחירים'
  ROW_FX       = 'חשיפה למט"ח'
  (ROW_DOMESTIC = 'נכסים בארץ' is present but per spec we compute it as 100-abroad)
"""

import hashlib
import io
import itertools
import math
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

# ──────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Profit Mix Optimizer",
    page_icon="📊",
    layout="wide",
)

# ──────────────────────────────────────────────────────────────
# Password gate
# ──────────────────────────────────────────────────────────────
def _password_gate():
    try:
        pw = st.secrets["APP_PASSWORD"]
    except (KeyError, FileNotFoundError):
        pw = ""          # dev-mode: no password required
    if not pw:
        return           # no password configured → skip gate

    if not st.session_state.get("auth_ok"):
        st.title("🔐 כניסה למערכת")
        entered = st.text_input("סיסמה", type="password", key="_pw")
        if st.button("כניסה", type="primary"):
            if entered == pw:
                st.session_state["auth_ok"] = True
                st.rerun()
            else:
                st.error("סיסמה שגויה – נסה שנית.")
        st.stop()

_password_gate()

# ──────────────────────────────────────────────────────────────
# CSS  –  Dark mode + RTL + slider tooltip fix
# ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── Global RTL ── */
html, body, [class*="css"] { direction: rtl; text-align: right; }
section.main > div        { direction: rtl; }
.block-container          { max-width: 1600px; padding-top: 1rem; }

/* ── Sliders: keep LTR internally so tooltip stays on-screen ── */
div[data-testid="stSlider"]                              { direction: ltr !important; }
div[data-testid="stSlider"] label,
div[data-testid="stSlider"] [data-testid="stWidgetLabel"]{ direction: rtl !important;
                                                           text-align: right !important;
                                                           width: 100% !important; }

/* ── Dark DataFrames (headers + cells) ── */
[data-testid="stDataFrame"]                      { direction: rtl; }
[data-testid="stDataFrame"] div[role="grid"]     { background:#0d0f14 !important;
                                                   border-radius:12px !important;
                                                   border:1px solid rgba(255,255,255,.08) !important; }
[data-testid="stDataFrame"] div[role="columnheader"]
                                                 { background:#141820 !important;
                                                   color:#e0e4f0 !important;
                                                   font-weight:700;
                                                   border-bottom:1px solid rgba(255,255,255,.12) !important; }
[data-testid="stDataFrame"] div[role="gridcell"] { background:#0d0f14 !important;
                                                   color:#e0e4f0 !important;
                                                   border-bottom:1px solid rgba(255,255,255,.05) !important; }
/* Light-bg cells → dark text so it stays readable */
[data-testid="stDataFrame"] div[role="gridcell"][style*="background-color: rgb(200"],
[data-testid="stDataFrame"] div[role="gridcell"][style*="background-color: rgb(1"] { color:#111 !important; }

/* ── KPI cards ── */
.kpi-row  { display:flex; gap:14px; margin:.5rem 0 1.4rem; flex-wrap:wrap; }
.kpi-card { flex:1; min-width:200px; background:#141820;
            border:1px solid rgba(255,255,255,.10); border-radius:16px; padding:14px 18px; }
.kpi-card.best   { border-color:#2eb87a; }
.kpi-card.second { border-color:#4a8fd1; }
.kpi-card.third  { border-color:#c98a2c; }
.kpi-title   { font-size:.88rem; opacity:.8; margin-bottom:4px; }
.kpi-score   { font-size:1.9rem; font-weight:800; }
.kpi-details { font-size:.80rem; opacity:.85; margin-top:6px; line-height:1.6; }

/* ── Misc ── */
.stButton > button { border-radius:12px; font-weight:700; }
</style>
""", unsafe_allow_html=True)

# ──────────────────────────────────────────────────────────────
# EXACT parameter row names (verified from the Excel file)
# ──────────────────────────────────────────────────────────────
ROW_EQUITY   = 'סך חשיפה למניות מתוך כלל נכסי הקופה'
ROW_ABROAD   = 'סך חשיפה לנכסים המושקעים בחו"ל מתוך כלל נכסי הקופה'
ROW_SHARPE   = 'מדד שארפ'
ROW_ILLIQUID = 'נכסים לא סחירים'
ROW_FX       = 'חשיפה למט"ח'

EXCEL_DEFAULT = 'קרנות_השתלמות_חשיפות.xlsx'

# ──────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Fund:
    sheet:    str
    name:     str
    provider: str
    equity:   float   # %
    abroad:   float   # %
    fx:       float   # %
    illiquid: float   # %
    sharpe:   float   # number (0 if missing)

# ──────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────
def _to_pct(x) -> float:
    """Parse '51.43%' → 51.43  |  0.5143 → 51.43  |  '1.24' → 1.24."""
    if x is None:
        return float('nan')
    if isinstance(x, (int, float, np.integer, np.floating)):
        v = float(x)
        # Fraction stored as 0..1 → convert to 0..100
        if 0.0 < abs(v) <= 1.0:
            return v * 100.0
        return v
    s = str(x).strip().replace(',', '')
    if s.endswith('%'):
        try:
            return float(s[:-1])
        except ValueError:
            return float('nan')
    try:
        v = float(s)
        if 0.0 < abs(v) <= 1.0:
            return v * 100.0
        return v
    except ValueError:
        return float('nan')


def _to_num(x) -> float:
    if x is None:
        return float('nan')
    if isinstance(x, (int, float, np.integer, np.floating)):
        return float(x)
    try:
        return float(str(x).strip().replace(',', ''))
    except ValueError:
        return float('nan')


def _provider(fund_name: str) -> str:
    """'כלל השתלמות כללי' → 'כלל'  |  'ילין לפידות קרן השתלמות ...' → 'ילין לפידות'."""
    if 'השתלמות' in fund_name:
        before = fund_name.split('השתלמות')[0].strip().rstrip(' -')
        if before.endswith('קרן'):
            before = before[:-3].strip()
        return before.strip() or fund_name.strip()
    return fund_name.strip()

# ──────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────
def load_funds(src) -> Tuple[List[Fund], List[str]]:
    """
    Read all sheets from the Excel source (path or file-like).
    Returns (list_of_Fund, log_lines).
    Skips only truly empty sheets or sheets without 'פרמטר' column.
    """
    logs: List[str] = []
    funds: List[Fund] = []

    xl = pd.ExcelFile(src)
    for sheet in xl.sheet_names:
        raw = pd.read_excel(xl, sheet_name=sheet)

        # Safety guard: skip if no 'פרמטר' column or completely empty
        if raw.empty or 'פרמטר' not in raw.columns:
            logs.append(f"⚠️  '{sheet}': אין עמודת 'פרמטר' – מדולג.")
            continue

        # Build param lookup: param_name → Series(fund_name → value)
        raw = raw.copy()
        raw['פרמטר'] = raw['פרמטר'].astype(str).str.strip()
        raw = raw[~raw['פרמטר'].isin(['None', 'nan', ''])]
        if raw.empty:
            logs.append(f"⚠️  '{sheet}': ריק לאחר ניקוי – מדולג.")
            continue

        # Deduplicate index (keep first)
        raw = raw.drop_duplicates(subset='פרמטר', keep='first')
        pmap = raw.set_index('פרמטר')  # param_name → fund_name → value

        def get_row(row_name: str) -> Optional[pd.Series]:
            if row_name in pmap.index:
                return pmap.loc[row_name]
            return None

        r_equity   = get_row(ROW_EQUITY)
        r_abroad   = get_row(ROW_ABROAD)
        r_sharpe   = get_row(ROW_SHARPE)
        r_illiquid = get_row(ROW_ILLIQUID)
        r_fx       = get_row(ROW_FX)

        if r_equity is None or r_abroad is None or r_illiquid is None:
            logs.append(
                f"⚠️  '{sheet}': חסרות שורות חיוניות "
                f"({'מניות' if r_equity is None else ''}/"
                f"{'חו\"ל' if r_abroad is None else ''}/"
                f"{'לא-סחיר' if r_illiquid is None else ''}) – מדולג."
            )
            continue

        n_added = 0
        for col in pmap.columns:
            fname = str(col).strip()
            if not fname or fname.lower() in ('none', 'nan', ''):
                continue

            equity   = _to_pct(r_equity.get(col))
            abroad   = _to_pct(r_abroad.get(col))
            illiquid = _to_pct(r_illiquid.get(col))
            fx       = _to_pct(r_fx.get(col))       if r_fx      is not None else float('nan')
            sharpe   = _to_num(r_sharpe.get(col))   if r_sharpe  is not None else float('nan')

            # Core fields must be present
            if any(math.isnan(v) for v in [equity, abroad, illiquid]):
                logs.append(f"  ⚠️  קרן '{fname}' בגיליון '{sheet}': חסרים נתוני ליבה – מדולגת.")
                continue

            # FX / sharpe may be missing → default 0
            if math.isnan(fx):
                fx = 0.0
            if math.isnan(sharpe):
                sharpe = 0.0

            funds.append(Fund(
                sheet=sheet, name=fname,
                provider=_provider(fname),
                equity=equity, abroad=abroad,
                fx=fx, illiquid=illiquid,
                sharpe=sharpe,
            ))
            n_added += 1

        logs.append(f"✅  גיליון '{sheet}': נטענו {n_added} קרנות.")

    return funds, logs


# ──────────────────────────────────────────────────────────────
# Caching wrappers
# ──────────────────────────────────────────────────────────────
@st.cache_data(show_spinner="טוען נתונים מהקובץ…")
def _load_from_path(path: str) -> Tuple[list, list]:
    return load_funds(path)


@st.cache_data(show_spinner="טוען נתונים מהקובץ שהועלה…")
def _load_from_bytes(md5: str, data: bytes) -> Tuple[list, list]:
    return load_funds(io.BytesIO(data))


def get_funds(src) -> Tuple[List[Fund], List[str]]:
    if isinstance(src, str):
        return _load_from_path(src)
    data = src.read()
    src.seek(0)
    return _load_from_bytes(hashlib.md5(data).hexdigest(), data)


def find_excel() -> Optional[str]:
    try:
        for fn in os.listdir('.'):
            if fn == EXCEL_DEFAULT:
                return fn
        for fn in os.listdir('.'):
            if fn.lower().endswith('.xlsx'):
                return fn
    except OSError:
        pass
    return None

# ──────────────────────────────────────────────────────────────
# Optimization
# ──────────────────────────────────────────────────────────────
def _blend(fs: List[Fund], ws: List[float]) -> Dict[str, float]:
    return {
        'equity':   sum(w * f.equity   for w, f in zip(ws, fs)),
        'abroad':   sum(w * f.abroad   for w, f in zip(ws, fs)),
        'fx':       sum(w * f.fx       for w, f in zip(ws, fs)),
        'illiquid': sum(w * f.illiquid for w, f in zip(ws, fs)),
        'sharpe':   sum(w * f.sharpe   for w, f in zip(ws, fs)),
    }


def _deviation(v: Dict, t: Dict, tw: Dict) -> float:
    return (  tw['equity']   * abs(v['equity']   - t['equity'])
            + tw['abroad']   * abs(v['abroad']   - t['abroad'])
            + tw['fx']       * abs(v['fx']       - t['fx'])
            + tw['illiquid'] * abs(v['illiquid'] - t['illiquid']))


def _svc(provs: List[str], ws: List[float],
         smap: Dict[str, float], dflt: float) -> float:
    return sum(w * smap.get(p, dflt) for w, p in zip(ws, provs))


def _score(dev: float, sharpe: float, svc: float,
           sharpe_w: float, service_w: float) -> float:
    """Lower = better."""
    return dev - sharpe_w * sharpe - service_w * (svc / 100.0)


def compute(
    funds:          List[Fund],
    target:         Dict[str, float],
    target_weights: Dict[str, float],
    sharpe_w:       float,
    service_w:      float,
    svc_map:        Dict[str, float],
    dflt_svc:       float,
    same_prov_only: bool,
    n:              int,            # 1, 2, or 3
) -> Tuple[List[Dict], str]:

    if len(funds) < n:
        return [], f"נדרשות לפחות {n} קרנות; יש רק {len(funds)}."

    candidates: List[Dict] = []

    # ── 1 fund ──────────────────────────────────────────────
    if n == 1:
        for f in funds:
            v   = _blend([f], [1.0])
            dev = _deviation(v, target, target_weights)
            svc = svc_map.get(f.provider, dflt_svc)
            sc  = _score(dev, v['sharpe'], svc, sharpe_w, service_w)
            candidates.append(dict(funds=[f], weights=[1.0],
                                   vals=v, deviation=dev, svc=svc, score=sc))

    # ── 2 funds ─────────────────────────────────────────────
    elif n == 2:
        grid = [i / 100.0 for i in range(0, 101)]
        for f1, f2 in itertools.combinations(funds, 2):
            if same_prov_only and f1.provider != f2.provider:
                continue
            best = None
            for w1 in grid:
                w2 = 1.0 - w1
                v   = _blend([f1, f2], [w1, w2])
                dev = _deviation(v, target, target_weights)
                svc = _svc([f1.provider, f2.provider], [w1, w2], svc_map, dflt_svc)
                sc  = _score(dev, v['sharpe'], svc, sharpe_w, service_w)
                if best is None or sc < best['score']:
                    best = dict(funds=[f1, f2], weights=[w1, w2],
                                vals=v, deviation=dev, svc=svc, score=sc)
            if best:
                candidates.append(best)

    # ── 3 funds ─────────────────────────────────────────────
    else:
        step  = 0.05
        grid1 = [round(i * step, 3) for i in range(int(1 / step) + 1)]
        for f1, f2, f3 in itertools.combinations(funds, 3):
            if same_prov_only and not (f1.provider == f2.provider == f3.provider):
                continue
            best = None
            for w1 in grid1:
                for w2 in grid1:
                    w3 = round(1.0 - w1 - w2, 3)
                    if w3 < -1e-9 or w3 > 1.0 + 1e-9:
                        continue
                    w3 = max(0.0, min(1.0, w3))
                    v   = _blend([f1, f2, f3], [w1, w2, w3])
                    dev = _deviation(v, target, target_weights)
                    svc = _svc([f1.provider, f2.provider, f3.provider],
                               [w1, w2, w3], svc_map, dflt_svc)
                    sc  = _score(dev, v['sharpe'], svc, sharpe_w, service_w)
                    if best is None or sc < best['score']:
                        best = dict(funds=[f1, f2, f3], weights=[w1, w2, w3],
                                    vals=v, deviation=dev, svc=svc, score=sc)
            if best:
                candidates.append(best)

    if not candidates:
        return [], "לא נמצאו שילובים תקינים. נסה להרחיב הגדרות או להפחית מגבלות."

    candidates.sort(key=lambda c: c['score'])

    # Pick 3 with unique providers across alternatives
    chosen:      List[Dict] = []
    used_prov:   set = set()
    used_names:  set = set()

    for c in candidates:
        prov_set  = {f.provider for f in c['funds']}
        names_set = {f.name     for f in c['funds']}
        if used_names & names_set:
            continue
        if not same_prov_only and (used_prov & prov_set):
            continue
        chosen.append(c)
        used_prov  |= prov_set
        used_names |= names_set
        if len(chosen) == 3:
            break

    # Fallback: relax uniqueness if needed
    if len(chosen) < 3:
        for c in candidates:
            if c in chosen:
                continue
            names_set = {f.name for f in c['funds']}
            if used_names & names_set:
                continue
            chosen.append(c)
            used_names |= names_set
            if len(chosen) == 3:
                break

    if not chosen:
        chosen = candidates[:3]

    return chosen, ""


# ──────────────────────────────────────────────────────────────
# Advantage text (shown in table)
# ──────────────────────────────────────────────────────────────
BADGES = ["🥇 מומלץ ראשי", "🥈 חלופה שנייה", "🥉 חלופה שלישית"]
CARD_CLS = ["best", "second", "third"]

def advantage_text(rank: int, dev: float, sharpe: float, svc: float) -> str:
    if rank == 0:
        return f"הכי קרוב ליעד – סטייה {dev:.1f}"
    if rank == 1:
        return f"שארפ {sharpe:.2f} + שירות {svc:.1f} | סטייה {dev:.1f}"
    return f"שירות משוקלל גבוה {svc:.1f} | סטייה {dev:.1f}"

# ──────────────────────────────────────────────────────────────
# App UI
# ──────────────────────────────────────────────────────────────
st.title("📊 Profit Mix Optimizer")
st.caption("בחר יעדי תמהיל וקבל 3 חלופות לשילוב קרנות השתלמות.")

# Sidebar
with st.sidebar:
    st.markdown("### 📊 Profit Mix Optimizer")
    st.caption("כלי לאופטימיזציה של תמהיל קרנות השתלמות.")
    st.divider()
    if st.button("🔄 איפוס הגדרות", use_container_width=True):
        for k in [k for k in st.session_state if k != 'auth_ok']:
            del st.session_state[k]
        st.rerun()
    st.divider()
    uploaded_xl = st.file_uploader("📂 החלפת קובץ Excel (אופציונלי)", type=['xlsx'])
    st.caption("ברירת מחדל: הקובץ שבריפו.")

excel_src = uploaded_xl if uploaded_xl is not None else find_excel()
if excel_src is None:
    st.error("❌ לא נמצא קובץ Excel. הוסף את הקובץ לריפו או העלה אחד.")
    st.stop()

funds, load_logs = get_funds(excel_src)

# ── TABS ────────────────────────────────────────────────────
tab_s, tab_r, tab_t = st.tabs(["⚙️ הגדרות יעד", "📊 תוצאות", "🔍 שקיפות / פירוט"])

# ══════════════════════════════════════════════════════════════
# TAB 1 – Settings
# ══════════════════════════════════════════════════════════════
with tab_s:
    # Presets
    st.markdown("#### ⚡ בחירה מהירה")
    pa, pb, pc, pd_ = st.columns(4)
    with pa:
        if st.button("🌍 גלובלי 60/40", use_container_width=True):
            st.session_state.update(ta=60, te=40, tf=30, ti=10)
            st.rerun()
    with pb:
        if st.button("💱 מקסימום מט\"ח", use_container_width=True):
            st.session_state.update(ta=60, te=40, tf=70, ti=10)
            st.rerun()
    with pc:
        if st.button("🏢 לא-סחיר עד 20%", use_container_width=True):
            st.session_state.update(ta=50, te=40, tf=25, ti=20)
            st.rerun()
    with pd_:
        if st.button("🛡️ שמרני", use_container_width=True):
            st.session_state.update(ta=40, te=20, tf=20, ti=5)
            st.rerun()

    st.divider()
    st.markdown("#### 🎯 יעדי תמהיל")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        t_abroad   = st.slider("יעד חו\"ל (%)", 0, 130, st.session_state.get('ta', 60))
    with c2:
        t_equity   = st.slider("יעד מניות (%)", 0, 130, st.session_state.get('te', 40))
    with c3:
        t_fx       = st.slider("יעד מט\"ח (%)", 0, 130, st.session_state.get('tf', 30))
    with c4:
        t_illiquid = st.slider("יעד לא-סחיר (%)", 0, 40, st.session_state.get('ti', 15))

    # Show computed Israel metric
    st.caption(f"📍 ישראל = 100 − חו\"ל = **{100 - t_abroad:.1f}%** (מחושב, לא מהנתונים)")

    st.markdown("#### ⚖️ חשיבות יחסית לכל יעד")
    w1, w2, w3, w4 = st.columns(4)
    with w1:
        wt_abroad   = st.slider("חשיבות חו\"ל",     0.0, 3.0, 1.0, 0.1)
    with w2:
        wt_equity   = st.slider("חשיבות מניות",    0.0, 3.0, 1.0, 0.1)
    with w3:
        wt_fx       = st.slider("חשיבות מט\"ח",    0.0, 3.0, 1.0, 0.1)
    with w4:
        wt_illiquid = st.slider("חשיבות לא-סחיר", 0.0, 3.0, 1.0, 0.1)

    st.markdown("#### 📈 שארפ ושירות")
    s1, s2 = st.columns(2)
    with s1:
        sharpe_w  = st.slider("משקל שארפ (גבוה = העדף שארפ)", 0.0, 5.0, 1.5, 0.1)
    with s2:
        service_w = st.slider("משקל שירות (גבוה = העדף שירות)", 0.0, 8.0, 4.0, 0.1,
                               help="שירות תמיד מקבל משקל גבוה בכל 3 החלופות")

    st.markdown("#### 🔧 אפשרויות נוספות")
    o1, o2, o3 = st.columns(3)
    with o1:
        n_funds = st.radio("שילוב של", [1, 2, 3], index=1,
                            format_func=lambda x: f"{x} מסלול{'ים' if x > 1 else ''}")
    with o2:
        same_prov = st.toggle("רק מאותו גוף מנהל", value=False,
                               help="אם מופעל – שתי/שלוש הקרנות בכל חלופה יהיו מאותו גוף")
    with o3:
        dflt_svc = st.slider("ציון שירות ברירת מחדל", 0, 100, 70,
                              help="ציון לגופים שאין להם ציון מותאם")

    st.markdown("#### 🏆 ציוני שירות (CSV)")
    svc_file = st.file_uploader("העלאת CSV: provider, score", type=["csv"], key="svc")

    # Template download
    tpl_providers = sorted({f.provider for f in funds}) if funds else \
        ['כלל', 'מנורה', 'הפניקס', 'מיטב', 'אנליסט', 'מגדל',
         'מור', 'הראל', 'ילין לפידות', 'אלטשולר שחם', 'אינפיניטי']
    tpl_df = pd.DataFrame({'provider': tpl_providers,
                            'score':    [70] * len(tpl_providers)})
    st.download_button("⬇️ הורד תבנית CSV לשירות",
                        tpl_df.to_csv(index=False).encode('utf-8-sig'),
                        'service_template.csv', 'text/csv')

    svc_map: Dict[str, float] = {}
    if svc_file is not None:
        try:
            svc_df = pd.read_csv(svc_file)
            for _, row in svc_df.iterrows():
                p  = str(row.get('provider', '')).strip()
                sc = _to_num(row.get('score', float('nan')))
                if p and not math.isnan(sc):
                    svc_map[p] = float(sc)
            st.success(f"✅ נטענו ציוני שירות ל-{len(svc_map)} גופים.")
        except Exception as e:
            st.error(f"שגיאה בקריאת CSV שירות: {e}")

    # Store computed targets in session
    TARGET = {'equity': float(t_equity), 'abroad': float(t_abroad),
              'fx': float(t_fx),         'illiquid': float(t_illiquid)}
    TW     = {'equity': float(wt_equity), 'abroad': float(wt_abroad),
              'fx': float(wt_fx),          'illiquid': float(wt_illiquid)}

    for k, v in dict(TARGET=TARGET, TW=TW,
                     sharpe_w=sharpe_w, service_w=service_w,
                     svc_map=svc_map, dflt_svc=dflt_svc,
                     same_prov=same_prov, n_funds=n_funds).items():
        st.session_state[k] = v

    st.divider()
    calc_btn = st.button("▶ חשב", type="primary", use_container_width=True)
    if calc_btn:
        st.session_state['compute_flag'] = True
        st.rerun()

# ══════════════════════════════════════════════════════════════
# TAB 2 – Results
# ══════════════════════════════════════════════════════════════
with tab_r:
    if not st.session_state.get('compute_flag'):
        st.info("⬅️ הגדר יעדים בטאב 'הגדרות יעד' ולחץ **▶ חשב**.")
    elif not funds:
        st.error("❌ לא נטענו קרנות מהקובץ! ראה פירוט בטאב 'שקיפות / פירוט'.")
    else:
        with st.spinner("מחשב שילובים אופטימליים…"):
            alts, err = compute(
                funds,
                target=st.session_state['TARGET'],
                target_weights=st.session_state['TW'],
                sharpe_w=st.session_state['sharpe_w'],
                service_w=st.session_state['service_w'],
                svc_map=st.session_state.get('svc_map', {}),
                dflt_svc=st.session_state.get('dflt_svc', 70),
                same_prov_only=st.session_state.get('same_prov', False),
                n=st.session_state.get('n_funds', 2),
            )

        if err:
            st.warning(err)
        elif not alts:
            st.warning("לא נמצאו חלופות.")
        else:
            tgt = st.session_state['TARGET']

            # ── KPI cards ────────────────────────────────────
            cards = '<div class="kpi-row">'
            for i, alt in enumerate(alts):
                v  = alt['vals']
                cards += f"""
                <div class="kpi-card {CARD_CLS[i]}">
                  <div class="kpi-title">{BADGES[i]}</div>
                  <div class="kpi-score">סטייה: {alt['deviation']:.1f}</div>
                  <div class="kpi-details">
                    מניות {v['equity']:.1f}% · חו"ל {v['abroad']:.1f}% ·
                    מט"ח {v['fx']:.1f}% · לא-סחיר {v['illiquid']:.1f}%<br/>
                    ישראל {100-v['abroad']:.1f}% · שארפ {v['sharpe']:.2f} · שירות {alt['svc']:.1f}
                  </div>
                </div>"""
            cards += '</div>'
            st.markdown(cards, unsafe_allow_html=True)

            # ── Full table ────────────────────────────────────
            nf = st.session_state.get('n_funds', 2)
            rows = []
            for i, alt in enumerate(alts):
                v   = alt['vals']
                row = {'חלופה': BADGES[i]}
                for j, (f, w) in enumerate(zip(alt['funds'], alt['weights']), 1):
                    row[f'מסלול #{j}']  = f.name
                    row[f'גיליון #{j}'] = f.sheet
                    row[f'גוף #{j}']    = f.provider
                    row[f'משקל #{j}']   = f"{w * 100:.1f}%"
                row['מניות (%)']   = f"{v['equity']:.1f}%"
                row['חו"ל (%)']    = f"{v['abroad']:.1f}%"
                row['ישראל (%)']   = f"{100 - v['abroad']:.1f}%"
                row['מט"ח (%)']    = f"{v['fx']:.1f}%"
                row['לא-סחיר (%)'] = f"{v['illiquid']:.1f}%"
                row['שארפ']        = f"{v['sharpe']:.2f}"
                row['שירות']       = f"{alt['svc']:.1f}"
                row['סטייה']       = f"{alt['deviation']:.1f}"
                row['יתרון']       = advantage_text(i, alt['deviation'],
                                                     v['sharpe'], alt['svc'])
                rows.append(row)

            df_out = pd.DataFrame(rows)
            col_cfg = {f'מסלול #{j}':  st.column_config.TextColumn(width='large')
                       for j in range(1, nf + 1)}
            col_cfg['יתרון'] = st.column_config.TextColumn(width='large')
            for j in range(1, nf + 1):
                col_cfg[f'גיליון #{j}'] = st.column_config.TextColumn(width='medium')

            st.dataframe(df_out, use_container_width=True, hide_index=True,
                         column_config=col_cfg)

# ══════════════════════════════════════════════════════════════
# TAB 3 – Transparency
# ══════════════════════════════════════════════════════════════
with tab_t:
    st.subheader("🔍 פירוט נתונים ולוג טעינה")

    if not funds:
        st.warning("לא נטענו קרנות. בדוק את הלוג למטה.")
    else:
        prov_list = sorted({f.provider for f in funds})
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("קרנות שנטענו", len(funds))
        col_b.metric("גופי ניהול", len(prov_list))
        col_c.metric("גיליונות", len({f.sheet for f in funds}))

        st.write("**גופים שנמצאו:**", ", ".join(prov_list))

        with st.expander("📋 לוג טעינה מפורט"):
            for line in load_logs:
                st.write(line)

        with st.expander("📄 כל הקרנות שנטענו"):
            df_all = pd.DataFrame([{
                'שם קרן':       f.name,
                'גוף':          f.provider,
                'גיליון':       f.sheet,
                'מניות':        f"{f.equity:.1f}%",
                'חו"ל':         f"{f.abroad:.1f}%",
                'ישראל':        f"{100 - f.abroad:.1f}%",
                'מט"ח':         f"{f.fx:.1f}%",
                'לא-סחיר':      f"{f.illiquid:.1f}%",
                'שארפ':         f"{f.sharpe:.2f}",
            } for f in sorted(funds, key=lambda x: x.provider)])

            st.dataframe(df_all, use_container_width=True, hide_index=True,
                         column_config={
                             'שם קרן':  st.column_config.TextColumn(width='large'),
                             'גיליון':  st.column_config.TextColumn(width='medium'),
                         })

    if 'TARGET' in st.session_state:
        with st.expander("🎯 יעדים ומשקולות נוכחיים"):
            st.json({'יעדים': st.session_state.get('TARGET', {}),
                     'משקולות': st.session_state.get('TW', {})})

    with st.expander("ℹ️ על הקוד ושיטת החישוב"):
        st.markdown("""
**שורות הפרמטרים המדויקות מה-Excel (תוקן מגרסאות קודמות):**
- מניות: `סך חשיפה למניות מתוך כלל נכסי הקופה`
- חו"ל:  `סך חשיפה לנכסים המושקעים בחו"ל מתוך כלל נכסי הקופה`
- שארפ:  `מדד שארפ`
- לא-סחיר: `נכסים לא סחירים`
- מט"ח:  `חשיפה למט"ח` (שורה 7 – גרסאות קודמות פספסו אותה!)

**ישראל** מחושב תמיד כ- `100 − חו"ל` (לא מהעמודה `נכסים בארץ`).

**אלגוריתם:**
- 2 קרנות: grid חיפוש על משקל 0%–100% בצעדים של 1% (101 ×101 = ~10,000 נקודות לכל זוג).
- 3 קרנות: simplex grid בצעדים של 5%.
- ציון: `סטייה_משוקללת − sharpe_w × שארפ − service_w × (שירות/100)`.
- גיוון: 3 החלופות נבחרות עם ספקים שונים בין החלופות (כשאפשר).
        """)
