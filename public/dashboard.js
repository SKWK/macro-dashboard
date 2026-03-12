/**
 * dashboard.js — Macro Early Warning Dashboard
 *
 * Responsibilities:
 *   1. Fetch macro.json
 *   2. Render 4 primary summary cards (Conditions, Forward Risk, Det. Speed, Confirmation)
 *   3. Render secondary legacy panels (Regime, Gauge, Credit Stress, Liquidity, Impulse)
 *   4. Render Deterioration Monitor alert cards
 *   5. Render tradable signals
 *   6. Render indicator table with z-score bars
 *   7. Render Chart.js historical charts
 */

'use strict';

// ---------------------------------------------------------------------------
// Series display metadata (supplements what comes from macro.json)
// ---------------------------------------------------------------------------
const GROUP_ORDER  = ['labor', 'credit', 'financial', 'lending', 'rates', 'liquidity'];
const GROUP_LABELS = {
  labor:     'Labor Market',
  credit:    'Credit Stress',
  financial: 'Financial Conditions',
  lending:   'Bank Lending',
  rates:     'Interest Rates / Yield Curve',
  liquidity: 'Liquidity',
};

// Chart line color per group
const GROUP_COLOR = {
  labor:     '#38bdf8',
  credit:    '#f97316',
  financial: '#a78bfa',
  lending:   '#fb7185',
  rates:     '#34d399',
  liquidity: '#67e8f9',
};

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function fmt(value, unit) {
  if (value === null || value === undefined || isNaN(Number(value))) return 'N/A';
  const n = Number(value);
  switch (unit) {
    case '$M':    return '$' + (n / 1_000_000).toFixed(2) + 'T';
    case '$B':    return '$' + n.toFixed(0) + 'B';
    case 'persons': return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : n.toFixed(0);
    case 'bps':   return n.toFixed(0) + ' bps';
    case '%':     return n.toFixed(2) + '%';
    case '% net': return n.toFixed(1) + '% net';
    case 'hours': return n.toFixed(1) + ' hrs';
    default:      return n.toFixed(3) + (unit ? ' ' + unit : '');
  }
}

function fmtChange(change, unit) {
  if (change === null || change === undefined || isNaN(Number(change))) return 'N/A';
  const n = Number(change);
  const sign = n > 0 ? '+' : '';
  switch (unit) {
    case '$M':    return sign + (n / 1_000_000).toFixed(3) + 'T';
    case '$B':    return sign + n.toFixed(1) + 'B';
    case 'persons': return sign + (n >= 1000 || n <= -1000 ? (n / 1000).toFixed(1) + 'k' : n.toFixed(0));
    case 'bps':   return sign + n.toFixed(0) + ' bps';
    case '%':     return sign + n.toFixed(3) + '%';
    case '% net': return sign + n.toFixed(2) + '% net';
    case 'hours': return sign + n.toFixed(2) + ' hrs';
    default:      return sign + n.toFixed(4) + (unit ? ' ' + unit : '');
  }
}

function fmtChartVal(value, unit) {
  // Concise value for chart card header
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  switch (unit) {
    case '$M':    return '$' + (n / 1_000_000).toFixed(2) + 'T';
    case '$B':    return '$' + n.toFixed(0) + 'B';
    case 'persons': return (n / 1000).toFixed(0) + 'k';
    case 'bps':   return n.toFixed(0);
    case '%':     return n.toFixed(2) + '%';
    case '% net': return n.toFixed(1) + '%';
    case 'hours': return n.toFixed(1);
    default:      return n.toFixed(2);
  }
}

// ---------------------------------------------------------------------------
// SVG Gauge
// ---------------------------------------------------------------------------

/**
 * Draw a semicircular gauge into the given SVG element.
 * probability: 0–100
 */
function drawGauge(svgEl, probability) {
  // Geometry
  const cx = 110, cy = 122, r = 90;
  const strokeW = 14;

  // Convert probability (0–100) to standard-math angle (radians)
  // 0%  → π   (left)
  // 50% → π/2 (top)
  // 100%→ 0   (right)
  function pToRad(p) {
    return Math.PI * (1 - Math.min(100, Math.max(0, p)) / 100);
  }

  // Polar → SVG coordinates (y-axis flipped)
  function pt(angle) {
    return [cx + r * Math.cos(angle), cy - r * Math.sin(angle)];
  }

  // Build SVG arc path from probability p1 to p2 (going left→right through top)
  function arcPath(p1, p2) {
    const [x1, y1] = pt(pToRad(p1));
    const [x2, y2] = pt(pToRad(p2));
    const large = (p2 - p1) > 50 ? 1 : 0;
    // sweep-flag = 1 → clockwise in SVG screen coords = goes through top
    return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
  }

  // Colored arc segments
  const segments = [
    { p1: 0,  p2: 25, color: '#22c55e' },  // green
    { p1: 25, p2: 50, color: '#f59e0b' },  // yellow
    { p1: 50, p2: 75, color: '#f97316' },  // orange
    { p1: 75, p2: 100,color: '#ef4444' },  // red
  ];

  // Needle tip
  const needleAngle  = pToRad(probability);
  const needleLen    = r - 12;
  const [nx, ny]     = [cx + needleLen * Math.cos(needleAngle), cy - needleLen * Math.sin(needleAngle)];

  // Needle base triangle points (for a slightly fat base)
  const baseHalf = 4;
  const perpAngle = needleAngle + Math.PI / 2;
  const [bx1, by1] = [cx + baseHalf * Math.cos(perpAngle), cy - baseHalf * Math.sin(perpAngle)];
  const [bx2, by2] = [cx - baseHalf * Math.cos(perpAngle), cy + baseHalf * Math.sin(perpAngle)];

  // Probability label color
  const pColor = probability >= 75 ? '#ef4444' : probability >= 50 ? '#f97316' : probability >= 25 ? '#f59e0b' : '#22c55e';

  // Tick marks at 0, 25, 50, 75, 100
  const tickOuter = r + 8;
  const tickInner = r - 3;
  const tickMarks = [0, 25, 50, 75, 100].map(p => {
    const ang = pToRad(p);
    const [ox, oy] = [cx + tickOuter * Math.cos(ang), cy - tickOuter * Math.sin(ang)];
    const [ix, iy] = [cx + tickInner * Math.cos(ang), cy - tickInner * Math.sin(ang)];
    const [lx, ly] = [cx + (tickOuter + 10) * Math.cos(ang), cy - (tickOuter + 10) * Math.sin(ang)];
    return { ox, oy, ix, iy, lx, ly, label: p + '%' };
  });

  let svg = '';

  // Background arc
  svg += `<path d="${arcPath(0, 100)}" fill="none" stroke="#1e2a3d" stroke-width="${strokeW + 2}" stroke-linecap="round"/>`;

  // Colored segments
  segments.forEach(s => {
    svg += `<path d="${arcPath(s.p1, s.p2)}" fill="none" stroke="${s.color}" stroke-width="${strokeW}" stroke-linecap="butt" opacity="0.75"/>`;
  });

  // Active fill up to current probability
  const activeColor = pColor;
  if (probability > 1) {
    svg += `<path d="${arcPath(0, probability)}" fill="none" stroke="${activeColor}" stroke-width="${strokeW - 4}" stroke-linecap="round" opacity="0.5"/>`;
  }

  // Tick marks
  tickMarks.forEach(t => {
    svg += `<line x1="${t.ox.toFixed(1)}" y1="${t.oy.toFixed(1)}" x2="${t.ix.toFixed(1)}" y2="${t.iy.toFixed(1)}" stroke="#263347" stroke-width="1.5"/>`;
  });

  // Needle
  svg += `<polygon points="${nx.toFixed(1)},${ny.toFixed(1)} ${bx1.toFixed(1)},${by1.toFixed(1)} ${bx2.toFixed(1)},${by2.toFixed(1)}" fill="white" opacity="0.9"/>`;

  // Center hub
  svg += `<circle cx="${cx}" cy="${cy}" r="5" fill="#0f1520" stroke="white" stroke-width="1.5"/>`;

  // Probability percentage — centred above the hub
  svg += `<text x="${cx}" y="${cy - 14}" text-anchor="middle" fill="${pColor}" font-size="22" font-weight="800" font-family="Inter, sans-serif">${probability.toFixed(0)}%</text>`;

  svgEl.innerHTML = svg;
}

// ---------------------------------------------------------------------------
// Regime panel
// ---------------------------------------------------------------------------

function renderRegime(data) {
  const nameEl    = document.getElementById('regime-name');
  const scoreEl   = document.getElementById('regime-score');
  const detailsEl = document.getElementById('regime-details');
  const panelEl   = document.getElementById('regime-panel');

  if (!nameEl) return;

  const regime  = data.macro_regime || '—';
  const score   = data.regime_score ?? '—';
  const details = data.regime_details || [];

  nameEl.textContent  = regime;
  scoreEl.textContent = score;

  detailsEl.innerHTML = '';
  details.slice(0, 5).forEach(d => {
    const li = document.createElement('li');
    li.textContent = d;
    detailsEl.appendChild(li);
  });

  const slug = regime.toLowerCase().replace(/\s+/g, '-').replace('recession-risk', 'recession');
  panelEl.className = `panel regime-panel regime-${slug}`;
  // Note: header badge is now controlled by renderForwardRisk (primary signal)
}

// ---------------------------------------------------------------------------
// Credit stress panel
// ---------------------------------------------------------------------------

function renderStress(data) {
  const level   = (data.credit_stress_level || 'Low').toLowerCase();
  const valueEl = document.getElementById('stress-value');
  const panelEl = document.getElementById('stress-panel');
  const bars    = document.querySelectorAll('.stress-bar');

  if (!valueEl) return;

  valueEl.textContent = data.credit_stress_level || '—';
  panelEl.className   = `panel stress-panel stress-${level}`;

  const activeCount = level === 'low' ? 1 : level === 'medium' ? 2 : 3;
  bars.forEach((bar, i) => {
    bar.className = 'stress-bar';
    if (i < activeCount) bar.classList.add(`active-${level}`);
  });
}

// ---------------------------------------------------------------------------
// Liquidity panel
// ---------------------------------------------------------------------------

function renderLiquidity(data) {
  const valueEl  = document.getElementById('liquidity-value');
  const indexEl  = document.getElementById('liquidity-index');
  const panelEl  = document.getElementById('liquidity-panel');

  if (!valueEl) return;

  const regime = data.liquidity_regime || '—';
  const index  = data.liquidity_index;

  valueEl.textContent = regime;
  indexEl.textContent = index !== null && index !== undefined
    ? (index >= 0 ? '+' : '') + Number(index).toFixed(2)
    : '—';

  const slug = regime.toLowerCase();
  panelEl.className = `panel liquidity-panel liquidity-${slug}`;
}

// ---------------------------------------------------------------------------
// Recession Confirmation panel + details
// ---------------------------------------------------------------------------

function renderRecessionConfirmation(data) {
  const label   = data.recession_confirmation         || '—';
  const score   = data.recession_confirmation_score   ?? '—';
  const details = data.recession_confirmation_details || {};

  const valueEl  = document.getElementById('confirm-value');
  const scoreEl  = document.getElementById('confirm-score');
  const subEl    = document.getElementById('confirm-sub');
  const panelEl  = document.getElementById('confirm-panel');
  const detailEl = document.getElementById('confirm-details');

  if (!valueEl) return;

  const slug = label.toLowerCase();
  const subText = {
    low:   'Labor market not yet confirming recession risk',
    watch: 'Some labor deterioration emerging',
    high:  'Labor market weakening is broad enough to confirm recession risk',
  }[slug] || '';

  valueEl.textContent = label;
  scoreEl.textContent = score;
  if (subEl)   subEl.textContent = subText;
  if (panelEl) panelEl.className = `panel confirm-panel confirm-${slug}`;

  if (!detailEl) return;

  const rows = [
    {
      label:     'Initial Claims (ICSA)',
      latest:    details.icsa_latest,
      avg13:     details.icsa_avg13,
      unit:      'persons',
      threshold: '≥8% above avg',
      triggered: !!details.icsa_triggered,
    },
    {
      label:     'Continuing Claims (CCSA)',
      latest:    details.ccsa_latest,
      avg13:     details.ccsa_avg13,
      unit:      'persons',
      threshold: '≥5% above avg',
      triggered: !!details.ccsa_triggered,
    },
    {
      label:     'Avg Weekly Hours (AWHAETP)',
      latest:    details.awhaetp_latest,
      avg13:     details.awhaetp_avg13,
      unit:      'hours',
      threshold: '≥0.1 hrs below avg',
      triggered: !!details.awhaetp_triggered,
    },
  ];

  detailEl.innerHTML = rows.map(row => {
    const status    = row.triggered ? 'triggered' : 'ok';
    const pillText  = row.triggered ? '▼ Triggered' : '▲ OK';
    const latestFmt = row.latest != null ? fmt(row.latest, row.unit) : '—';
    const avgFmt    = row.avg13  != null ? fmt(row.avg13,  row.unit) : '—';
    return `
      <div class="confirm-row ${status}">
        <div class="confirm-row-top">
          <span class="confirm-row-label">${row.label}</span>
          <span class="confirm-row-pill ${status}">${pillText}</span>
        </div>
        <div class="confirm-row-metrics">
          <div class="confirm-metric">
            <span class="metric-label">Latest</span>
            <span class="metric-val${row.triggered ? ' bad' : ''}">${latestFmt}</span>
          </div>
          <div class="confirm-metric">
            <span class="metric-label">13-Wk Avg</span>
            <span class="metric-val">${avgFmt}</span>
          </div>
          <div class="confirm-metric">
            <span class="metric-label">Threshold</span>
            <span class="metric-val">${row.threshold}</span>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

// ---------------------------------------------------------------------------
// Primary summary cards: Current Conditions, Forward Recession Risk,
// Deterioration Speed
// ---------------------------------------------------------------------------

function renderCurrentConditions(data) {
  const cc      = data.current_conditions || {};
  const label   = cc.label   || '—';
  const score   = cc.score   ?? '—';
  const drivers = cc.drivers || [];

  const labelEl   = document.getElementById('conditions-label');
  const scoreEl   = document.getElementById('conditions-score');
  const driversEl = document.getElementById('conditions-drivers');
  const panelEl   = document.getElementById('conditions-panel');

  if (!labelEl) return;

  labelEl.textContent = label;
  if (scoreEl)   scoreEl.textContent = score;
  if (panelEl)   panelEl.className   = `panel conditions-panel conditions-${label.toLowerCase()}`;
  if (driversEl) driversEl.innerHTML = drivers.map(d => `<li>${d}</li>`).join('');
}

function renderForwardRisk(data) {
  const fr      = data.forward_recession_risk || {};
  const label   = fr.label   || '—';
  const score   = fr.score   ?? '—';
  const drivers = fr.drivers || [];

  const labelEl   = document.getElementById('fwd-risk-label');
  const scoreEl   = document.getElementById('fwd-risk-score');
  const driversEl = document.getElementById('fwd-risk-drivers');
  const panelEl   = document.getElementById('fwd-risk-panel');
  const badgeEl   = document.getElementById('regime-badge');

  if (!labelEl) return;

  const slug = label.toLowerCase();
  labelEl.textContent = label;
  if (scoreEl)   scoreEl.textContent = score;
  if (panelEl)   panelEl.className   = `panel fwd-risk-panel fwd-risk-${slug}`;
  if (driversEl) driversEl.innerHTML = drivers.map(d => `<li>${d}</li>`).join('');

  // Forward Recession Risk is the primary headline — update the header badge
  if (badgeEl) {
    badgeEl.textContent = label;
    badgeEl.className   = `badge ${slug}`;
  }
}

function renderDeteriorationSpeed(data) {
  const ds      = data.deterioration_speed || {};
  const label   = ds.label   || '—';
  const count   = ds.count   ?? '—';
  const drivers = ds.drivers || [];

  const labelEl   = document.getElementById('detspeed-label');
  const countEl   = document.getElementById('detspeed-count');
  const driversEl = document.getElementById('detspeed-drivers');
  const panelEl   = document.getElementById('detspeed-panel');

  if (!labelEl) return;

  const slug = label.toLowerCase();
  labelEl.textContent = label;
  if (countEl)   countEl.textContent = count;
  if (panelEl)   panelEl.className   = `panel detspeed-panel detspeed-${slug}`;
  if (driversEl) driversEl.innerHTML = drivers.map(d => `<li>${d}</li>`).join('');
}

// ---------------------------------------------------------------------------
// Global Liquidity panel
// ---------------------------------------------------------------------------

function renderGlobalLiquidity(data) {
  const gl      = data.global_liquidity || {};
  const cls     = gl.classification || '—';
  const drivers = gl.drivers || [];

  const valueEl   = document.getElementById('global-liq-value');
  const liq1mEl   = document.getElementById('global-liq-1m');
  const liq3mEl   = document.getElementById('global-liq-3m');
  const driversEl = document.getElementById('global-liq-drivers');
  const panelEl   = document.getElementById('global-liq-panel');

  if (!valueEl) return;

  const slug = cls.toLowerCase();
  valueEl.textContent = cls;
  if (panelEl) panelEl.className = `panel global-liq-panel global-liq-${slug}`;

  const fmt1m = gl.change_1m != null
    ? (gl.change_1m >= 0 ? '+' : '') + '$' + Math.abs(gl.change_1m).toFixed(0) + 'B'
    : '—';
  const fmt3m = gl.change_3m != null
    ? (gl.change_3m >= 0 ? '+' : '') + '$' + Math.abs(gl.change_3m).toFixed(0) + 'B'
    : '—';

  const sign1m = gl.change_1m != null ? (gl.change_1m >= 0 ? 'up' : 'down') : '';
  const sign3m = gl.change_3m != null ? (gl.change_3m >= 0 ? 'up' : 'down') : '';

  if (liq1mEl) { liq1mEl.textContent = fmt1m; liq1mEl.className = `global-liq-change-val ${sign1m}`; }
  if (liq3mEl) { liq3mEl.textContent = fmt3m; liq3mEl.className = `global-liq-change-val ${sign3m}`; }
  if (driversEl) driversEl.innerHTML = drivers.map(d => `<li>${d}</li>`).join('');
}

// ---------------------------------------------------------------------------
// Credit Impulse panel
// ---------------------------------------------------------------------------

function renderCreditImpulse(data) {
  const ci       = data.credit_impulse || {};
  const valueEl  = document.getElementById('impulse-value');
  const amountEl = document.getElementById('impulse-amount');
  const subEl    = document.getElementById('impulse-sub');
  const panelEl  = document.getElementById('impulse-panel');

  if (!valueEl) return;

  const classification = ci.classification || 'Unknown';
  const slug = classification.toLowerCase();

  const subText = {
    positive: 'Credit growth is accelerating relative to last year',
    neutral:  'Credit growth is stable',
    negative: 'Credit growth is slowing relative to last year',
  }[slug] || '';

  valueEl.textContent  = classification;
  if (amountEl) amountEl.textContent = ci.value != null ? fmt(ci.value, '$B') : '—';
  if (subEl)    subEl.textContent    = subText;
  if (panelEl)  panelEl.className    = `panel impulse-panel impulse-${slug}`;
}

// ---------------------------------------------------------------------------
// Tradable signals
// ---------------------------------------------------------------------------

function renderSignals(signals) {
  const grid = document.getElementById('signals-grid');
  if (!grid) return;

  if (!signals || signals.length === 0) {
    grid.innerHTML = '<p class="placeholder">No signals available.</p>';
    return;
  }

  grid.innerHTML = '';
  signals.forEach(sig => {
    const dir  = sig.direction || 'neutral';
    const card = document.createElement('div');
    card.className = `signal-card ${dir}`;
    card.innerHTML = `
      <div class="signal-header">
        <span class="signal-asset">${sig.asset}</span>
        <span class="signal-pill ${dir}">${dir === 'bullish' ? '▲ Bullish' : '▼ Bearish'}</span>
      </div>
      <p class="signal-rationale">${sig.rationale}</p>
    `;
    grid.appendChild(card);
  });
}

// ---------------------------------------------------------------------------
// Indicator table
// ---------------------------------------------------------------------------

function renderTable(indicators) {
  const tbody = document.getElementById('indicator-tbody');
  if (!tbody) return;

  if (!indicators || indicators.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="placeholder">No data.</td></tr>';
    return;
  }

  // Group indicators
  const groups = {};
  GROUP_ORDER.forEach(g => { groups[g] = []; });
  indicators.forEach(ind => {
    const g = ind.group || 'other';
    if (!groups[g]) groups[g] = [];
    groups[g].push(ind);
  });

  tbody.innerHTML = '';

  GROUP_ORDER.forEach(group => {
    const rows = groups[group];
    if (!rows || rows.length === 0) return;

    // Group header
    const header = document.createElement('tr');
    header.className = 'group-row';
    header.innerHTML = `<td colspan="6">${GROUP_LABELS[group] || group}</td>`;
    tbody.appendChild(header);

    rows.forEach(ind => {
      const unit         = ind.unit || '';
      const higherIsBad  = ind.higher_is_bad;
      const chg          = ind.period_change;
      const trend        = ind.trend_3m || 'neutral';
      const z            = ind.z_score ?? 0;

      // Change color class
      let chgClass = 'chg-neutral';
      if (chg !== null && chg !== undefined && higherIsBad !== null) {
        if (chg > 0) chgClass = higherIsBad ? 'chg-negative' : 'chg-positive';
        if (chg < 0) chgClass = higherIsBad ? 'chg-positive' : 'chg-negative';
      }

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${ind.label || ind.series_id}</td>
        <td class="series-code">${ind.series_id}</td>
        <td class="num-val">${fmt(ind.latest_value, unit)}</td>
        <td class="num-val ${chgClass}">${fmtChange(chg, unit)}</td>
        <td style="text-align:center">${trendBadge(trend)}</td>
        <td>${zScoreCell(z, higherIsBad)}</td>
      `;
      tbody.appendChild(tr);
    });
  });
}

function trendBadge(trend) {
  const icons = { improving: '▲', deteriorating: '▼', neutral: '—' };
  const icon  = icons[trend] || '—';
  return `<span class="trend-badge ${trend}">${icon} ${trend}</span>`;
}

function zScoreCell(z, higherIsBad) {
  // Clamp display range to ±3
  const MAX_Z   = 3;
  const clamped = Math.min(MAX_Z, Math.max(-MAX_Z, z));
  const pct     = (Math.abs(clamped) / MAX_Z) * 50; // max 50% of track width

  // Positive z → bar extends right from center; negative → left
  let fillClass = 'neutral';
  if (higherIsBad !== null) {
    if (z > 0.3)  fillClass = higherIsBad ? 'risk' : 'safe';
    if (z < -0.3) fillClass = higherIsBad ? 'safe' : 'risk';
  }

  const leftPos  = z >= 0 ? 50 : 50 - pct;
  const width    = pct;
  const signChar = z >= 0 ? '+' : '';

  return `
    <div class="z-cell">
      <div class="z-track">
        <div class="z-center-tick"></div>
        <div class="z-fill ${fillClass}" style="left:${leftPos.toFixed(1)}%; width:${width.toFixed(1)}%"></div>
      </div>
      <span class="z-label">${signChar}${z.toFixed(2)}</span>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Regime Shift / Macro Alerts
// ---------------------------------------------------------------------------

function renderRegimeShift(data) {
  const prob     = data.regime_shift_probability || 'LOW';
  const count    = data.regime_shift_count ?? 0;
  const alerts   = data.macro_alerts        || [];
  const signals  = data.regime_shift_signals || [];

  // Shift wrapper color class
  const wrapper = document.getElementById('shift-wrapper');
  if (wrapper) wrapper.className = `shift-wrapper shift-${prob.toLowerCase()}`;

  // Probability label
  const probEl = document.getElementById('shift-prob-value');
  if (probEl) probEl.textContent = prob;

  // Deteriorating count
  const countEl = document.getElementById('shift-count');
  if (countEl) countEl.textContent = count;

  // Dot indicators (5 dots, fill left → right)
  const dotsEl = document.getElementById('shift-dots');
  if (dotsEl) {
    dotsEl.innerHTML = '';
    for (let i = 0; i < 5; i++) {
      const dot = document.createElement('span');
      dot.className = 'shift-dot' + (i < count ? ' active' : '');
      dotsEl.appendChild(dot);
    }
  }

  // Alert cards
  const grid = document.getElementById('alerts-grid');
  if (grid && alerts.length > 0) {
    grid.innerHTML = '';
    alerts.forEach(alert => {
      const status   = alert.deteriorating ? 'deteriorating' : 'stable';
      const pillText = alert.deteriorating ? '▼ Deteriorating' : '▲ Stable';
      const unit     = alert.unit || '';

      // Format absolute changes using existing fmt helpers
      const chg1m = alert.change_1m !== null && alert.change_1m !== undefined
        ? fmtChange(alert.change_1m, unit) : '—';
      const chg3m = alert.change_3m !== null && alert.change_3m !== undefined
        ? fmtChange(alert.change_3m, unit) : '—';
      const momZ  = alert.momentum_z !== null
        ? (alert.momentum_z >= 0 ? '+' : '') + Number(alert.momentum_z).toFixed(2) : '—';

      // Color metric values by direction
      const momClass = alert.deteriorating ? 'bad' : (Math.abs(alert.momentum_z) < 0.3 ? '' : 'good');
      const chg1mClass = alert.deteriorating ? 'bad' : '';

      const card = document.createElement('div');
      card.className = `alert-card ${status}`;
      card.innerHTML = `
        <div class="alert-card-top">
          <span class="alert-label">${alert.label}</span>
          <span class="alert-pill ${status}">${pillText}</span>
        </div>
        <p class="alert-message">${alert.deteriorating ? alert.message : 'Watch for: ' + alert.message}</p>
        <div class="alert-metrics">
          <div class="alert-metric">
            <span class="metric-label">1M Chg</span>
            <span class="metric-val ${chg1mClass}">${chg1m}</span>
          </div>
          <div class="alert-metric">
            <span class="metric-label">3M Chg</span>
            <span class="metric-val">${chg3m}</span>
          </div>
          <div class="alert-metric">
            <span class="metric-label">Mom Z</span>
            <span class="metric-val ${momClass}">${momZ}</span>
          </div>
        </div>
      `;
      grid.appendChild(card);
    });
  }

  // Shift trade signals (reuse existing signal card markup + styles)
  const sigGrid = document.getElementById('shift-signals-grid');
  if (sigGrid) {
    if (!signals.length) {
      sigGrid.innerHTML = '<p class="placeholder">No signals available.</p>';
      return;
    }
    sigGrid.innerHTML = '';
    signals.forEach(sig => {
      const dir      = sig.direction || 'neutral';
      const pillText = dir === 'bullish' ? '▲ Bullish'
                     : dir === 'bearish' ? '▼ Bearish'
                     : '◆ Neutral';
      const card = document.createElement('div');
      card.className = `signal-card ${dir}`;
      card.innerHTML = `
        <div class="signal-header">
          <span class="signal-asset">${sig.asset}</span>
          <span class="signal-pill ${dir}">${pillText}</span>
        </div>
        <p class="signal-rationale">${sig.rationale}</p>
      `;
      sigGrid.appendChild(card);
    });
  }
}

// ---------------------------------------------------------------------------
// Historical charts (Chart.js)
// ---------------------------------------------------------------------------

// Keep chart instances to avoid duplicates on re-render
const _charts = {};

function renderCharts(indicators) {
  const grid = document.getElementById('charts-grid');
  if (!grid) return;

  const validIndicators = indicators.filter(
    ind => ind.history_dates && ind.history_dates.length > 1
  );

  if (validIndicators.length === 0) {
    grid.innerHTML = '<p class="placeholder">Run the Python script to generate historical data.</p>';
    return;
  }

  grid.innerHTML = '';

  validIndicators.forEach(ind => {
    const canvasId  = `chart-${ind.series_id}`;
    const color     = GROUP_COLOR[ind.group] || '#94a3b8';
    const unit      = ind.unit || '';
    const chg       = ind.period_change;
    const higherIsBad = ind.higher_is_bad;

    let chgClass = '';
    if (chg !== null && chg !== undefined && higherIsBad !== null) {
      if (chg > 0) chgClass = higherIsBad ? 'down' : 'up';
      if (chg < 0) chgClass = higherIsBad ? 'up'   : 'down';
    }
    const chgStr = chg !== null && chg !== undefined ? fmtChange(chg, unit) : '';

    const card = document.createElement('div');
    card.className = 'chart-card';
    card.innerHTML = `
      <div class="chart-card-header">
        <div class="chart-card-label">${ind.label}</div>
        <div class="chart-card-meta">
          <span class="chart-card-value">${fmtChartVal(ind.latest_value, unit)}</span>
          ${chgStr ? `<span class="chart-card-change ${chgClass}">${chgStr}</span>` : ''}
        </div>
      </div>
      <div class="chart-canvas-wrap">
        <canvas id="${canvasId}"></canvas>
      </div>
    `;
    grid.appendChild(card);

    // Destroy previous instance
    if (_charts[canvasId]) {
      _charts[canvasId].destroy();
      delete _charts[canvasId];
    }

    const ctx = document.getElementById(canvasId);
    if (!ctx) return;

    // Gradient fill
    const gradient = ctx.getContext('2d').createLinearGradient(0, 0, 0, 110);
    gradient.addColorStop(0, hexToRgba(color, 0.25));
    gradient.addColorStop(1, hexToRgba(color, 0.0));

    // Thin down dates — show ~8 labels regardless of data density
    const dates  = ind.history_dates;
    const values = ind.history_values;
    const step   = Math.max(1, Math.floor(dates.length / 8));
    const labels = dates.map((d, i) => {
      if (i % step !== 0 && i !== dates.length - 1) return '';
      return formatDateLabel(d);
    });

    _charts[canvasId] = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data:            values,
          borderColor:     color,
          backgroundColor: gradient,
          borderWidth:     1.5,
          pointRadius:     0,
          pointHoverRadius: 4,
          fill:            true,
          tension:         0.3,
        }],
      },
      options: {
        responsive:          true,
        maintainAspectRatio: false,
        animation:           { duration: 600 },
        interaction:         { mode: 'index', intersect: false },
        plugins: {
          legend:  { display: false },
          tooltip: {
            backgroundColor: '#172030',
            titleColor:      '#94a3b8',
            bodyColor:       '#e2e8f0',
            borderColor:     '#263347',
            borderWidth:     1,
            callbacks: {
              title: items => formatDateLabel(dates[items[0].dataIndex]),
              label: item => fmt(item.raw, unit),
            },
          },
        },
        scales: {
          x: {
            grid:  { color: 'rgba(255,255,255,0.04)', drawBorder: false },
            ticks: { color: '#4a5568', font: { size: 10, family: 'JetBrains Mono' }, maxRotation: 0 },
          },
          y: {
            grid:  { color: 'rgba(255,255,255,0.04)', drawBorder: false },
            ticks: { color: '#4a5568', font: { size: 10, family: 'JetBrains Mono' },
                     callback: v => fmtChartVal(v, unit) },
          },
        },
      },
    });
  });
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function formatDateLabel(dateStr) {
  if (!dateStr) return '';
  const d = new Date(dateStr + 'T00:00:00');
  return d.toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
}

function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

// ---------------------------------------------------------------------------
// Main loader
// ---------------------------------------------------------------------------

async function loadDashboard() {
  // Mark as loading
  const dot = document.getElementById('status-dot');
  const upd = document.getElementById('updated-label');

  try {
    const res = await fetch('macro.json?' + Date.now());
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    // Header
    if (dot) dot.classList.add('live');
    if (upd) upd.textContent = data.updated ? `Updated ${data.updated}` : '';

    // ── Primary summary cards ──────────────────────────────────────
    renderCurrentConditions(data);
    renderForwardRisk(data);         // also sets header badge
    renderDeteriorationSpeed(data);
    renderRecessionConfirmation(data);
    renderGlobalLiquidity(data);

    // ── Secondary legacy panels ───────────────────────────────────
    renderRegime(data);
    renderStress(data);
    renderLiquidity(data);
    renderCreditImpulse(data);

    // Gauge
    const gaugeSvg = document.getElementById('gauge-svg');
    if (gaugeSvg) drawGauge(gaugeSvg, data.recession_probability ?? 0);

    // ── Deterioration Monitor ─────────────────────────────────────
    renderRegimeShift(data);

    // Signals
    renderSignals(data.tradable_signals || []);

    // Table
    renderTable(data.indicators || []);

    // Charts — run after table so DOM is ready
    renderCharts(data.indicators || []);

  } catch (err) {
    console.error('Dashboard load error:', err);
    if (upd) upd.textContent = 'Error: ' + err.message;
    if (dot) dot.style.background = '#ef4444';
  }
}

// Boot
loadDashboard();
