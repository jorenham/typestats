document$.subscribe(async () => {
  const root = document.getElementById("detail-root")
  if (!root) return

  const pkg = getPackageFromHash()
  if (!pkg) {
    showUploadZone(root)
    return
  }

  try {
    const manifest = await fetchManifest()
    const entry = manifest[pkg]
    if (!entry) {
      showError(root, `Package <code>${escapeHtml(pkg)}</code> not found in manifest.`)
      return
    }
    const version = entry.latest
    const report = await fetchReport(pkg, version)
    await renderDetail(root, report, entry, version)
  } catch (err) {
    showError(root, `Failed to load report: ${escapeHtml(err instanceof Error ? err.message : err)}`)
  }
})

function showUploadZone(root) {
  const pageH1 = document.querySelector("h1")
  if (pageH1) pageH1.textContent = "View report"

  root.innerHTML = `<div class="upload-zone" tabindex="0" role="button" aria-label="Upload a JSON report file">
    <p>Drop a <code>.json</code> report here, or click to select a file.</p>
    <p class="upload-hint">Generate one with <code>typestats report &lt;package&gt; &gt; report.json</code></p>
    <input type="file" accept=".json,application/json" hidden>
  </div>
  <details class="paste-section">
    <summary>Or paste JSON directly</summary>
    <label for="paste-json" class="sr-only">Paste typestats JSON report</label>
    <textarea id="paste-json" class="paste-area" placeholder="Paste typestats JSON report here..."></textarea>
    <button class="paste-btn" type="button">Load report</button>
  </details>`

  const zone = root.querySelector(".upload-zone")
  const input = zone.querySelector("input[type=file]")
  const pasteBtn = root.querySelector(".paste-btn")
  const pasteArea = root.querySelector(".paste-area")

  zone.addEventListener("click", () => input.click())
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault()
      input.click()
    }
  })
  input.addEventListener("change", () => {
    if (input.files.length) handleUpload(root, input.files[0])
  })

  zone.addEventListener("dragover", (e) => {
    e.preventDefault()
    zone.classList.add("upload-zone--hover")
  })
  zone.addEventListener("dragleave", () => {
    zone.classList.remove("upload-zone--hover")
  })
  zone.addEventListener("drop", (e) => {
    e.preventDefault()
    zone.classList.remove("upload-zone--hover")
    const file = e.dataTransfer.files[0]
    if (file) handleUpload(root, file)
  })

  pasteBtn.addEventListener("click", () => {
    handlePaste(root, pasteArea.value)
  })
}

async function handleUpload(root, file) {
  root.innerHTML = "<p>Loading report...</p>"
  try {
    const text = await file.text()
    let report
    try {
      report = JSON.parse(text)
    } catch {
      showUploadError(root, `<code>${escapeHtml(file.name)}</code> is not valid JSON.`)
      return
    }
    if (!report.package || !report.version || !report.module_reports) {
      showUploadError(root, `<code>${escapeHtml(file.name)}</code> is not a valid typestats report (missing required fields).`)
      return
    }
    const warn = schemaWarning(report)
    await renderDetail(root, report, null, report.version, warn)
  } catch (err) {
    showUploadError(root, `Failed to render report: ${escapeHtml(err instanceof Error ? err.message : err)}`)
  }
}

async function handlePaste(root, text) {
  if (!text.trim()) return
  root.innerHTML = "<p>Loading report...</p>"
  try {
    let report
    try {
      report = JSON.parse(text)
    } catch {
      showUploadError(root, "Pasted text is not valid JSON.")
      return
    }
    if (!report.package || !report.version || !report.module_reports) {
      showUploadError(root, "Pasted JSON is not a valid typestats report (missing required fields).")
      return
    }
    const warn = schemaWarning(report)
    await renderDetail(root, report, null, report.version, warn)
  } catch (err) {
    showUploadError(root, `Failed to render report: ${escapeHtml(err instanceof Error ? err.message : err)}`)
  }
}

function showUploadError(root, message) {
  showError(root, message)
  const retry = document.createElement("p")
  retry.innerHTML = `<a href="#" class="upload-retry">Try again</a>`
  retry.querySelector("a").addEventListener("click", (e) => {
    e.preventDefault()
    showUploadZone(root)
  })
  root.appendChild(retry)
}

function schemaWarning(report) {
  const root = document.getElementById("detail-root")
  const schemaVersion = Number(root.dataset.schemaVersion)
  const minVersion = root.dataset.minTypestatsVersion
  const v = report.schema_version
  if ((v ?? schemaVersion) >= schemaVersion) return null
  return `This report was generated with an older version of <code>typestats</code>. `
    + `Please upgrade to <code>typestats>=${minVersion}</code> and regenerate it.`
}

async function renderDetail(root, report, manifestEntry, version, warning = null) {
  const pkg = report.package
  const baseVer = report.base_version
  const hasDiff = manifestEntry && manifestEntry.versions.length >= 2

  document.title = `${pkg} ${version} - typestats`

  const pageH1 = document.querySelector("h1")
  if (pageH1) {
    let heading = `${pkg} ${version}`
    if (baseVer) heading += ` (${baseVer})`
    pageH1.textContent = heading
  }

  const parts = []
  if (warning) {
    parts.push(`<div class="admonition warning"><p class="admonition-title">Outdated report</p><p>${warning}</p></div>`)
  }
  const navLinks = []
  if (hasDiff) navLinks.push(`<a href="../history/#${encodeURIComponent(pkg)}">Version history</a>`)
  if (manifestEntry) {
    const jsonUrl = `${DATA_BASE_URL}/${encodeURIComponent(pkg)}/${encodeURIComponent(version)}.json`
    navLinks.push(`<a href="${jsonUrl}">Download JSON</a>`)
  }
  if (navLinks.length) parts.push(`<p>${navLinks.join(" | ")}</p>`)
  parts.push(renderGridCards(report))
  parts.push(renderModulesTable(report))
  parts.push(renderIncompleteAnnotations(report))
  parts.push(renderTypeIgnores(report))

  root.innerHTML = parts.join("\n")

  const smooth = !matchMedia("(prefers-reduced-motion: reduce)").matches
  for (const a of root.querySelectorAll("[data-scroll-to]")) {
    a.addEventListener("click", () => {
      const el = document.getElementById(a.dataset.scrollTo)
      if (el) el.scrollIntoView({ behavior: smooth ? "smooth" : "auto" })
    })
  }

  buildToc(root)
  await renderMermaidIn(root)

  initTablesort(root)
}

function renderGridCards(report) {
  const urls = extractProjectUrls(report.package, report.metadata)
  const pyTyped = report.py_typed
  const stubsLabel = STUBS_ONLY_LABEL[report.stubs_only] || ""

  const cov = coverage(report.n_typed, report.n_any, report.n_typable)
  const covStrict = coverage(report.n_typed, report.n_any, report.n_typable, true)

  const symbolsByKind = computeSymbolsByKind(report)

  let urlsHtml = `<tr><td>PyPI</td><td><a href="${urls.pypi}">${urls.pypi}</a></td></tr>`
  if (urls.repo) {
    urlsHtml += `<tr><td>Repository</td><td><a href="${urls.repo}">${urls.repo}</a></td></tr>`
  }
  const card1 = `<table>${urlsHtml}</table>`

  let pyTypedHtml = `<tr><td><code>py.typed</code></td><td>${iconPyTyped(pyTyped)}</td></tr>`
  if (stubsLabel) {
    pyTypedHtml += `<tr><td>stubs-only</td><td>${stubsLabel}</td></tr>`
  }
  const card2 = `<table>${pyTypedHtml}</table>`

  const covPie = mermaidPie([
    { label: "Typed", value: report.n_typed },
    { label: "Any", value: report.n_any },
    { label: "Untyped", value: report.n_untyped },
  ])
  const metricsBase = "../../reference/metrics/"
  const card3 = `<strong>Coverage</strong><hr>${covPie}
    <ul>
      <li>${fmtPct(cov)} <abbr title="Percentage of typed symbols">coverage</abbr> ${helpLink(metricsBase, "What is coverage?")}</li>
      <li>${fmtPct(covStrict)} <abbr title="Percentage of typed symbols, excluding Any">coverage (strict)</abbr> ${helpLink(metricsBase, "What is strict coverage?")}</li>
      <li>${fmtInt(report.n_typable)} typable
        <ul>
          <li>${fmtInt(report.n_typed)} typed</li>
          <li>${fmtInt(report.n_untyped)} untyped</li>
          <li>${fmtInt(report.n_any)} <code>Any</code></li>
        </ul>
      </li>
    </ul>`

  const kindPie = mermaidPie([
    { label: "functions", value: symbolsByKind.functions },
    { label: "classes", value: symbolsByKind.classes },
    { label: "other", value: symbolsByKind.attrs },
  ])
  const card4 = `<strong>Typables</strong><hr>${kindPie}
    <ul>
      <li>${fmtInt(report.n_functions)} functions (+${fmtInt(report.n_function_overloads - report.n_functions)} overloads)
        <ul><li>${fmtInt(report.n_function_params)} parameters</li></ul>
      </li>
      <li>${fmtInt(report.n_classes)} classes
        <ul>
          <li>${fmtInt(report.n_methods)} methods (+${fmtInt(report.n_method_overloads - report.n_methods)} overloads)
            <ul><li>${fmtInt(report.n_method_params)} parameters</li></ul>
          </li>
          <li>${fmtInt(report.n_properties)} properties</li>
        </ul>
      </li>
      <li>${fmtInt(report.n_modules)} modules
        <ul><li>${fmtInt(symbolsByKind.attrs)} attrs</li></ul>
      </li>
    </ul>`

  return `<div class="grid cards">
    <div class="card">${card1}</div>
    <div class="card">${card2}</div>
    <div class="card">${card3}</div>
    <div class="card">${card4}</div>
  </div>`
}

function computeSymbolsByKind(report) {
  const totals = { functions: 0, classes: 0, attrs: 0 }
  const kind2key = { function: "functions", attr: "attrs", property: "classes" }
  for (const m of report.module_reports) {
    for (const s of m.symbol_reports) {
      if (s.kind === "class") {
        for (const method of s.methods || []) totals.classes += method.n_typable
        for (const prop of s.properties || []) totals.classes += prop.n_typable
      } else {
        totals[kind2key[s.kind]] += s.n_typable
      }
    }
  }
  return totals
}

function moduleSlug(displayName) {
  return `module-${displayName.replace(/[^\w.-]/g, "")}`
}

function renderModulesTable(report) {
  const pkg = report.package
  const sorted = [...report.module_reports].sort((a, b) => a.path.localeCompare(b.path))

  const incompleteSlugs = {}
  for (const m of sorted) {
    if (hasIncompleteAnnotations(m)) {
      const displayName = displayModuleName(m.name, pkg)
      incompleteSlugs[displayName] = moduleSlug(displayName)
    }
  }

  const metricsBase = "../../reference/metrics/"
  let html = `<h2>Modules</h2>
  <table>
    <thead><tr>
      <th>Module</th>
      <th style="text-align:right"><abbr title="Percentage of typed symbols">Coverage</abbr> ${helpLink(metricsBase, "What is coverage?")}</th>
      <th style="text-align:right"><abbr title="Percentage of typed symbols, excluding Any">Coverage (strict)</abbr> ${helpLink(metricsBase, "What is strict coverage?")}</th>
      <th style="text-align:right"><abbr title="Number of public typable slots: each function parameter, return type, and variable counts as one">Typables</abbr> ${helpLink(metricsBase, "What are typables?")}</th>
      <th style="text-align:right"><abbr title="Number of type-checker ignore comments">Ignores</abbr></th>
    </tr></thead>
    <tbody>`

  for (const m of sorted) {
    const displayName = displayModuleName(m.name, pkg)
    const slug = incompleteSlugs[displayName]
    const cov = coverage(m.n_typed, m.n_any, m.n_typable)
    const covStrict = coverage(m.n_typed, m.n_any, m.n_typable, true)

    let nameCell = `<code>${displayName}</code>`
    if (slug) {
      nameCell += ` <a data-scroll-to="${slug}" title="Incomplete annotations" style="cursor:pointer">${iconIncomplete()}</a>`
    }

    html += `<tr>
      <td>${nameCell}</td>
      <td style="text-align:right">${fmtPct(cov)}</td>
      <td style="text-align:right">${fmtPct(covStrict)}</td>
      <td style="text-align:right">${m.n_typable}</td>
      <td style="text-align:right">${m.n_type_ignores}</td>
    </tr>`
  }

  html += "</tbody></table>"
  return html
}

function hasIncompleteAnnotations(moduleReport) {
  for (const s of moduleReport.symbol_reports) {
    if (s.n_untyped > 0 || s.n_any > 0) return true
  }
  return false
}

function renderIncompleteAnnotations(report) {
  const pkg = report.package
  const sorted = [...report.module_reports].sort((a, b) => a.path.localeCompare(b.path))
  const sections = []

  for (const m of sorted) {
    const rows = incompleteRows(m)
    if (!rows.length) continue
    const displayName = displayModuleName(m.name, pkg)
    const slug = moduleSlug(displayName)
    const nUntyped = rows.reduce((s, r) => s + r.n_untyped, 0)
    const nAny = rows.reduce((s, r) => s + r.n_any, 0)
    sections.push({ displayName, slug, nUntyped, nAny, rows })
  }

  let html = "<h2>Incomplete Annotations</h2>"

  if (!sections.length) {
    html += "<p>All symbols are fully typed. :tada:</p>"
    return html
  }

  for (const sec of sections) {
    html += `<span id="${sec.slug}"></span>
    <details>
      <summary><code>${sec.displayName}</code> (${sec.nUntyped} missing, ${sec.nAny} any)</summary>
      <table>
        <thead><tr>
          <th></th>
          <th>Symbol</th>
          <th style="text-align:right"><abbr title="Total annotation slots">Typable</abbr></th>
          <th style="text-align:right"><abbr title="Slots with a type annotation (including Any)">Typed</abbr></th>
          <th style="text-align:right"><abbr title="Slots typed as Any">Any</abbr></th>
        </tr></thead>
        <tbody>`

    for (const row of sec.rows) {
      const kindBadge = kindLabel(row.kind)
      html += `<tr>
        <td>${kindBadge}</td>
        <td><code>${row.name}</code></td>
        <td style="text-align:right">${row.n_typed + row.n_any + row.n_untyped}</td>
        <td style="text-align:right">${row.n_typed + row.n_any}</td>
        <td style="text-align:right">${row.n_any}</td>
      </tr>`
    }

    html += "</tbody></table></details>"
  }

  return html
}

function incompleteRows(moduleReport) {
  const rows = []
  for (const s of moduleReport.symbol_reports) {
    if (s.n_untyped === 0 && s.n_any === 0) continue

    const shortName = s.name.startsWith(moduleReport.name + ".")
      ? s.name.slice(moduleReport.name.length + 1)
      : s.name

    if (s.kind === "class") {
      for (const member of [...(s.methods || []), ...(s.properties || []), ...(s.attrs || [])]) {
        if (member.n_untyped === 0 && member.n_any === 0) continue
        rows.push({
          name: member.name,
          kind: member.kind === "function" ? "method" : member.kind,
          n_typed: member.n_typed,
          n_any: member.n_any,
          n_untyped: member.n_untyped,
        })
      }
      continue
    }

    rows.push({
      name: shortName,
      kind: s.kind,
      n_typed: s.n_typed,
      n_any: s.n_any,
      n_untyped: s.n_untyped,
    })
  }
  return rows
}

function kindLabel(kind) {
  if (kind === "function") return '<code class="sym-func">func</code>'
  if (kind === "method") return '<code class="sym-meth">meth</code>'
  if (kind === "property") return '<code class="sym-attr">prop</code>'
  return '<code class="sym-attr">attr</code>'
}

function renderTypeIgnores(report) {
  const counts = new Map()
  for (const m of report.module_reports) {
    for (const ic of m.type_ignores || []) {
      const label = ignoreLabel(ic)
      counts.set(label, (counts.get(label) || 0) + 1)
    }
  }

  let html = "<h2>Type-Ignore Comments</h2>"

  if (!counts.size) {
    html += "<p>No type-ignore comments.</p>"
    return html
  }

  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))

  html += `<table>
    <thead><tr>
      <th><abbr title="Type-checker ignore directive">Flavor</abbr></th>
      <th style="text-align:right">Count</th>
    </tr></thead>
    <tbody>`

  for (const [flavor, count] of sorted) {
    html += `<tr>
      <td><code>${flavor}</code></td>
      <td style="text-align:right">${count}</td>
    </tr>`
  }

  html += "</tbody></table>"
  return html
}
