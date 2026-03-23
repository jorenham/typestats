const MONTH_ABBR = [
  "",
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
]

document$.subscribe(async () => {
  const root = document.getElementById("diff-root")
  if (!root) return

  const pkg = getPackageFromHash()
  if (!pkg) {
    showError(root, "No package specified. Use a URL like <code>history/#numpy</code>.")
    return
  }

  try {
    const manifest = await fetchManifest()
    const entry = manifest[pkg]
    if (!entry) {
      showError(root, `Package <code>${escapeHtml(pkg)}</code> not found in manifest.`)
      return
    }

    if (entry.versions.length < 2) {
      showError(
        root,
        `Only one version available for <code>${escapeHtml(pkg)}</code>. Version history requires at least two versions.`,
      )
      return
    }

    const reports = await Promise.all(entry.versions.map(v => fetchReport(pkg, v)))

    renderDiff(root, pkg, reports)
  } catch (err) {
    showError(
      root,
      `Failed to load version history: ${escapeHtml(err instanceof Error ? err.message : err)}`,
    )
  }
})

function renderDiff(root, pkg, reports) {
  document.title = `${pkg} Version History - typestats`

  const pageH1 = document.querySelector("h1")
  if (pageH1) pageH1.textContent = `${pkg} Version History`

  const parts = []
  parts.push(renderChart(reports))
  parts.push(renderVersionTable(reports))

  root.innerHTML = parts.join("\n")

  initTablesort(root)
}

function renderChart(reports) {
  const covRaw = reports.map(r => coverage(r.n_typed, r.n_any, r.n_typable) * 100)
  const strictRaw = reports.map(
    r => coverage(r.n_typed, r.n_any, r.n_typable, true) * 100,
  )

  const dates = []
  for (const r of reports) {
    if (r.pypi && r.pypi.upload_time) {
      dates.push(r.pypi.upload_time.slice(0, 10))
    }
  }

  let labels, covData, strictData
  if (dates.length === reports.length && reports.length >= 2) {
    ;[labels, covData, strictData] = monthlySeries(dates, covRaw, strictRaw)
  } else {
    labels = reports.map(r => r.version)
    covData = covRaw.map(round1)
    strictData = strictRaw.map(round1)
  }

  if (labels.length < 2) return ""

  const chartHtml = svgLineChart(labels, covData, strictData)
  const legend = `<div class="chart-legend">
    <span class="chart-legend-item"><span class="chart-legend-swatch chart-legend-swatch--cov"></span> Coverage</span>
    <span class="chart-legend-item"><span class="chart-legend-swatch chart-legend-swatch--strict"></span> Coverage (strict)</span>
  </div>`

  return `<div class="admonition info"><p class="admonition-title">Coverage timeline</p>${chartHtml}${legend}</div>`
}

function svgLineChart(labels, covData, strictData) {
  const W = 800
  const H = 180
  const pad = { top: 20, right: 20, bottom: 50, left: 50 }
  const plotW = W - pad.left - pad.right
  const plotH = H - pad.top - pad.bottom
  const n = labels.length

  function x(i) {
    return pad.left + (i / (n - 1)) * plotW
  }
  function y(v) {
    return pad.top + plotH - (v / 100) * plotH
  }

  function polyline(data, color) {
    const points = data.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ")
    return `<polyline points="${points}" stroke="${color}"/>`
  }

  let yAxis = ""
  for (const v of [0, 25, 50, 75, 100]) {
    const yy = y(v)
    yAxis += `<line x1="${pad.left}" y1="${yy}" x2="${W - pad.right}" y2="${yy}"/>`
    yAxis += `<text x="${pad.left - 8}" y="${yy + 4}" text-anchor="end">${v}%</text>`
  }

  let xAxis = ""
  const step = Math.max(1, Math.floor(n / 8))
  for (let i = 0; i < n; i += step) {
    xAxis += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${labels[i]}</text>`
  }
  if ((n - 1) % step !== 0) {
    xAxis += `<text x="${x(n - 1)}" y="${H - 8}" text-anchor="middle">${labels[n - 1]}</text>`
  }

  return `<svg viewBox="0 0 ${W} ${H}" class="line-chart" preserveAspectRatio="xMidYMid meet">
    ${yAxis}${xAxis}
    ${polyline(covData, "#81c784")}
    ${polyline(strictData, "#ffb74d")}
  </svg>`
}

/** Bucket by month (max per month), forward-fill gaps. */
function monthlySeries(dateStrings, cov, strict) {
  const buckets = new Map()
  for (let i = 0; i < dateStrings.length; i++) {
    const [y, m] = dateStrings[i].split("-").map(Number)
    const key = `${y}-${m}`
    const prev = buckets.get(key)
    if (prev) {
      buckets.set(key, [Math.max(prev[0], cov[i]), Math.max(prev[1], strict[i])])
    } else {
      buckets.set(key, [cov[i], strict[i]])
    }
  }

  const firstDate = dateStrings[0].split("-").map(Number)
  const lastDate = dateStrings[dateStrings.length - 1].split("-").map(Number)
  let [year, month] = [firstDate[0], firstDate[1]]
  const [endYear, endMonth] = [lastDate[0], lastDate[1]]

  const labels = []
  const outCov = []
  const outStrict = []
  let lastC = cov[0]
  let lastS = strict[0]

  while (year < endYear || (year === endYear && month <= endMonth)) {
    labels.push(`${MONTH_ABBR[month]} ${year}`)
    const key = `${year}-${month}`
    if (buckets.has(key)) {
      ;[lastC, lastS] = buckets.get(key)
    }
    outCov.push(round1(lastC))
    outStrict.push(round1(lastS))

    month++
    if (month > 12) {
      month = 1
      year++
    }
  }

  return [labels, outCov, outStrict]
}

function round1(v) {
  return Math.round(v * 10) / 10
}

function renderVersionTable(reports) {
  const rows = []
  for (let i = 0; i < reports.length; i++) {
    const r = reports[i]
    const prev = i > 0 ? reports[i - 1] : null
    rows.push({
      version: r.version,
      baseVersion: r.base_version,
      releaseDate: r.pypi && r.pypi.upload_time ? r.pypi.upload_time.slice(0, 10) : "",
      coverage: covDelta(r, prev, false),
      coverageStrict: covDelta(r, prev, true),
      typables: intDelta(r.n_typable, prev ? prev.n_typable : null, { neutral: true }),
      untyped: intDelta(r.n_untyped, prev ? prev.n_untyped : null, {
        preferLower: true,
      }),
      ignores: intDelta(r.n_type_ignores, prev ? prev.n_type_ignores : null, {
        preferLower: true,
      }),
    })
  }
  rows.reverse()

  const pkg = reports[0].package
  let html = `<table>
    <thead><tr>
      <th data-sort-method="none">Version</th>
      <th style="text-align:right"><abbr title="Release date on PyPI">Released</abbr></th>
      <th style="text-align:right"><abbr title="Percentage of typed symbols">Coverage</abbr></th>
      <th style="text-align:right"><abbr title="Percentage of typed symbols, excluding Any">Coverage (strict)</abbr></th>
      <th style="text-align:right"><abbr title="Number of public typable slots">Typables</abbr></th>
      <th style="text-align:right"><abbr title="Slots without a type annotation">Untyped</abbr></th>
      <th style="text-align:right"><abbr title="Number of type-checker ignore comments">Ignores</abbr></th>
    </tr></thead>
    <tbody>`

  for (let i = 0; i < rows.length; i++) {
    const row = rows[i]
    let verCell = row.version
    if (row.baseVersion) verCell += ` (${row.baseVersion})`
    if (i === 0) {
      verCell = `<a href="../report/#${encodeURIComponent(pkg)}">${verCell}</a>`
    }

    html += `<tr>
      <td>${verCell}</td>
      <td style="text-align:right">${row.releaseDate}</td>
      ${metricCell(row.coverage)}
      ${metricCell(row.coverageStrict)}
      ${metricCell(row.typables)}
      ${metricCell(row.untyped)}
      ${metricCell(row.ignores)}
    </tr>`
  }

  html += "</tbody></table>"
  return html
}

function covDelta(r, prev, strict) {
  const val = coverage(r.n_typed, r.n_any, r.n_typable, strict)
  if (!prev) return { value: fmtPct(val) }
  const prevVal = coverage(prev.n_typed, prev.n_any, prev.n_typable, strict)
  const deltaPp = (val - prevVal) * 100
  if (!deltaPp) return { value: fmtPct(val) }
  const sign = deltaPp > 0 ? "+" : ""
  return {
    value: fmtPct(val),
    delta: `(${sign}${deltaPp.toFixed(1)}%)`,
    color: deltaPp > 0 ? "green" : "red",
  }
}

function intDelta(val, prevVal, { preferLower = false, neutral = false } = {}) {
  if (prevVal == null) return { value: String(val) }
  const delta = val - prevVal
  if (delta === 0) return { value: String(val) }
  const sign = delta > 0 ? "+" : ""
  let color = null
  if (!neutral) {
    color = delta < 0 === preferLower ? "green" : "red"
  }
  return { value: String(val), delta: `(${sign}${delta})`, color }
}

function metricCell(cell) {
  let inner = cell.value
  if (cell.delta != null) {
    inner += "<br>"
    if (cell.color) {
      inner += `<span style="color:${cell.color}">${cell.delta}</span>`
    } else {
      inner += cell.delta
    }
  }
  return `<td style="text-align:right">${inner}</td>`
}
