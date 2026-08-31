const DATA_BASE_URL =
  "https://raw.githubusercontent.com/jorenham/typestats/data/reports"

const REPO_HOSTS = new Set([
  "github.com",
  "gitlab.com",
  "bitbucket.org",
  "codeberg.org",
  "sr.ht",
])

const STUBS_RE = /^(?:(.+)-stubs|types-(.+))$/

function coverage(nTyped, nAny, nTypable, strict = false) {
  if (!nTypable) return 0
  const typed = strict ? nTyped : nTyped + nAny
  return typed / nTypable
}

function fmtPct(ratio) {
  return `${(ratio * 100).toFixed(1)}%`
}

function fmtInt(n) {
  return Number(n).toLocaleString("en")
}

function extractProjectUrls(pkg, metadata) {
  const urls = { pypi: `https://pypi.org/project/${pkg}/` }
  if (!metadata) return urls

  for (const header of ["Home-page", "Project-URL"]) {
    for (const entry of metadata[header] || []) {
      const url = entry.split(",").pop().trim()
      if (!url) continue
      try {
        const parsed = new URL(url)
        if (!REPO_HOSTS.has(parsed.hostname)) continue
        const parts = parsed.pathname.split("/").slice(0, 3)
        urls.repo = `https://${parsed.hostname}${parts.join("/")}`
        return urls
      } catch {
        /* malformed URL */
      }
    }
  }
  return urls
}

function displayModuleName(moduleName, packageName) {
  const m = STUBS_RE.exec(packageName)
  if (!m) return moduleName
  const base = m[1] || m[2]
  const stubsPrefix = moduleName.split(".")[0]
  if (moduleName.startsWith(stubsPrefix)) {
    return base + moduleName.slice(stubsPrefix.length)
  }
  return moduleName
}

// Adapt a `pyrefly coverage report` into the PackageReport shape the renderer expects.
function isPyreflyReport(raw) {
  return (
    Boolean(raw) &&
    Array.isArray(raw.module_reports) &&
    Boolean(raw.summary) &&
    raw.package == null
  )
}

function pyreflyTopPackage(modules) {
  const tops = new Set(
    modules.map(m => String(m.name || "").split(".")[0]).filter(Boolean),
  )
  return tops.size === 1 ? [...tops][0] : ""
}

const PYREFLY_LEAF_KINDS = new Set(["attr", "function", "property"])

function pyreflyLeaf(sym, name) {
  // attrs are single slots: clamp each count to 0/1
  const clamp = sym.kind === "attr" ? n => Math.min(n, 1) : n => n
  const n_typed = clamp(sym.n_typed)
  const n_any = clamp(sym.n_any)
  const n_untyped = clamp(sym.n_untyped)
  return {
    kind: sym.kind,
    name,
    line_start: sym.location ? sym.location.line : null,
    n_typed,
    n_any,
    n_untyped,
    n_typable: n_typed + n_any + n_untyped,
  }
}

function convertPyreflyModule(pm) {
  const prefix = pm.name + "."
  const short = name => (name.startsWith(prefix) ? name.slice(prefix.length) : name)
  const symbols = pm.symbol_reports || []

  const classes = new Map()
  for (const s of symbols) if (s.kind === "class") classes.set(s.name, s)

  const members = new Map()
  const topLevel = []
  for (const s of symbols) {
    if (s.kind === "class") continue
    if (!PYREFLY_LEAF_KINDS.has(s.kind)) {
      console.warn(`Skipping unexpected pyrefly symbol kind: ${s.kind}`)
      continue
    }
    const parent = s.name.slice(0, s.name.lastIndexOf("."))
    if (classes.has(parent)) {
      if (!members.has(parent)) members.set(parent, [])
      members.get(parent).push(s)
    } else {
      topLevel.push(s)
    }
  }

  const symbol_reports = topLevel.map(s => pyreflyLeaf(s, short(s.name)))
  for (const [fqn, cls] of classes) {
    const mem = members.get(fqn) || []
    const leaves = mem.map(s => pyreflyLeaf(s, short(s.name)))
    const sum = key => leaves.reduce((acc, m) => acc + (m[key] || 0), 0)
    symbol_reports.push({
      kind: "class",
      name: short(cls.name),
      line_start: cls.location ? cls.location.line : null,
      methods: leaves.filter(m => m.kind === "function"),
      properties: leaves.filter(m => m.kind === "property"),
      attrs: leaves.filter(m => m.kind === "attr"),
      n_typed: sum("n_typed"),
      n_any: sum("n_any"),
      n_untyped: sum("n_untyped"),
      n_typable: sum("n_typable"),
    })
  }

  return {
    name: pm.name,
    path: pm.path,
    n_typed: pm.n_typed,
    n_any: pm.n_any,
    n_untyped: pm.n_untyped,
    n_typable: pm.n_typable,
    n_type_ignores: pm.n_type_ignores,
    symbol_reports,
    type_ignores: (pm.type_ignores || []).map(ti => ({
      kind: ti.kind,
      rules: ti.codes ?? null,
    })),
  }
}

function normalizePyreflyReport(raw) {
  const modules = raw.module_reports.map(convertPyreflyModule)
  const summary = raw.summary || {}
  return {
    ...summary,
    schema_version: raw.schema_version,
    package: pyreflyTopPackage(raw.module_reports),
    version: null,
    base_version: null,
    stubs_only: "no",
    py_typed: null,
    pypi: null,
    metadata: null,
    module_reports: modules,
    n_modules: summary.n_modules ?? modules.length,
  }
}

function mermaidPie(slices) {
  const lines = [
    "---",
    "config:",
    "  theme: base",
    "  themeVariables:",
    '    pieOuterStrokeWidth: "1px"',
    '    pieStrokeWidth: "1px"',
    "---",
    "pie",
  ]
  for (const s of slices) {
    if (s.value) lines.push(`    "${s.label}" : ${Number(s.value)}`)
  }
  return `<pre class="mermaid">${lines.join("\n")}</pre>`
}

async function renderMermaidIn(root) {
  const nodes = root.querySelectorAll("pre.mermaid:not([data-processed])")
  if (!nodes.length) return

  if (typeof mermaid === "undefined") {
    await new Promise((resolve, reject) => {
      const s = document.createElement("script")
      s.src = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"
      s.onload = resolve
      s.onerror = reject
      document.head.appendChild(s)
    })
    mermaid.initialize({ startOnLoad: false, theme: "neutral" })
  }

  await mermaid.run({ nodes })
}

function ignoreLabel(ic) {
  let out = `${ic.kind}: ignore`
  if (ic.rules && ic.rules.length) {
    out += `[${[...ic.rules].sort().join(", ")}]`
  }
  return out
}

const STUBS_ONLY_LABEL = {
  no: "",
  "yes (third party)": "third-party",
  "yes (typeshed)": "typeshed",
}

function iconPyTyped(pyTyped) {
  const val = pyTyped.toLowerCase()
  if (val === "yes") return iconSpan("check-circle", "#4caf50")
  if (val === "no") return iconSpan("close-circle", "#e53935")
  if (val === "partial") return iconSpan("progress-check", "#fb8c00")
  if (val === "stubs") return iconSpan("check-circle-outline", "#4caf50")
  return ""
}

function iconSpan(name, color) {
  const symbols = {
    "check-circle": "\u2705",
    "close-circle": "\u274c",
    "progress-check": "\u2611\ufe0f",
    "check-circle-outline": "\u2714\ufe0f",
    "arrow-bottom-right": "\u2198\ufe0f",
  }
  const sym = symbols[name] || "\u2022"
  return `<span style="color:${color}" title="${name}">${sym}</span>`
}

function iconIncomplete() {
  return iconSpan("arrow-bottom-right", "currentColor")
}

function helpLink(href, title = "") {
  return `<a href="${href}" title="${title}" aria-label="${title}" class="help-link">?</a>`
}

async function fetchManifest() {
  const resp = await fetch("../manifest.json")
  if (!resp.ok) throw new Error(`Failed to fetch manifest: ${resp.status}`)
  return resp.json()
}

// Stored reports are gzipped, uploaded ones are not: sniff the magic bytes.
async function decodeReportText(buf) {
  const bytes = new Uint8Array(buf)
  if (bytes[0] !== 0x1f || bytes[1] !== 0x8b) return new TextDecoder().decode(buf)
  const gunzip = new DecompressionStream("gzip")
  return new Response(new Blob([buf]).stream().pipeThrough(gunzip)).text()
}

async function fetchReport(pkg, version) {
  const resp = await fetch(
    `${DATA_BASE_URL}/${encodeURIComponent(pkg)}/${encodeURIComponent(version)}.json.gz`,
  )
  if (!resp.ok)
    throw new Error(`Failed to fetch report for ${pkg}@${version}: ${resp.status}`)
  return JSON.parse(await decodeReportText(await resp.arrayBuffer()))
}

const TOP_PYPI_PACKAGES_URL =
  "https://hugovk.dev/top-pypi-packages/top-pypi-packages-30-days.min.json"

// Fill each `.pypi-downloads` cell with its `data-package`'s monthly downloads.
async function fillDownloadCells(cells) {
  if (!cells.length) return

  // dataset updates on the 1st of each month; use previous month's key until then
  const month = new Date()
  if (month.getDate() < 2) month.setMonth(month.getMonth() - 1)
  const cacheKey = `pypi-downloads-${month.getFullYear()}-${month.getMonth()}`

  let downloads
  try {
    const cached = localStorage.getItem(cacheKey)
    if (cached) downloads = new Map(JSON.parse(cached))
  } catch {
    // ignore storage errors
  }

  if (!downloads) {
    try {
      const resp = await fetch(TOP_PYPI_PACKAGES_URL)
      if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`)
      const { rows } = await resp.json()
      downloads = new Map(rows.map(r => [r.project.toLowerCase(), r.download_count]))
    } catch (err) {
      console.error("Failed to fetch PyPI download stats:", err)
      return
    }
    try {
      localStorage.setItem(cacheKey, JSON.stringify([...downloads]))
    } catch {
      // ignore storage quota errors
    }
  }

  const fmt = new Intl.NumberFormat("en", { notation: "compact" })
  for (const cell of cells) {
    const pkg = cell.dataset.package
    const count = pkg ? downloads.get(pkg.toLowerCase()) : null
    if (count == null) continue
    cell.textContent = fmt.format(count)
    cell.setAttribute("data-sort", String(count))
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
}

function getPackageFromHash() {
  const hash = location.hash.replace(/^#/, "")
  try {
    return decodeURIComponent(hash) || null
  } catch {
    return null
  }
}

function showError(root, message) {
  root.innerHTML = `<div class="admonition failure"><p class="admonition-title">Error</p><p>${message}</p></div>`
}

function initTablesort(root) {
  if (typeof Tablesort === "undefined") return
  root.querySelectorAll("table:not([class]):not([data-no-sort])").forEach(t => {
    const noSort = []
    t.querySelectorAll("th").forEach(th => {
      if (th.textContent.trim() === "Version") {
        th.setAttribute("data-sort-method", "none")
        noSort.push(th)
      }
    })
    new Tablesort(t)
    noSort.forEach(th => th.removeAttribute("role"))
  })
}

function buildToc(root) {
  const nav = document.querySelector(".md-sidebar--secondary nav")
  if (!nav) return

  const headings = root.querySelectorAll("h2")
  if (!headings.length) return

  const list = document.createElement("ul")
  list.className = "md-nav__list"

  for (const h of headings) {
    const li = document.createElement("li")
    li.className = "md-nav__item"
    const a = document.createElement("a")
    a.className = "md-nav__link"
    a.textContent = h.textContent.trim()
    a.style.cursor = "pointer"
    a.addEventListener("click", () => {
      const smooth = !matchMedia("(prefers-reduced-motion: reduce)").matches
      h.scrollIntoView({ behavior: smooth ? "smooth" : "auto" })
    })
    li.appendChild(a)
    list.appendChild(li)
  }

  nav.innerHTML = `<label class="md-nav__title" for="__toc">On this page</label>`
  nav.appendChild(list)
}
