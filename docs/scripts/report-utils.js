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
  return n.toLocaleString("en")
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
      } catch { /* malformed URL */ }
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

function mermaidPie(slices) {
  const lines = ["pie"]
  for (const s of slices) {
    if (s.value) lines.push(`    "${s.label}" : ${s.value}`)
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

const STUBS_ONLY_LABEL = { NO: "", THIRD_PARTY: "third-party", TYPESHED: "typeshed" }

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

async function fetchReport(pkg, version) {
  const resp = await fetch(`${DATA_BASE_URL}/${encodeURIComponent(pkg)}/${encodeURIComponent(version)}.json`)
  if (!resp.ok) throw new Error(`Failed to fetch report for ${pkg}@${version}: ${resp.status}`)
  return resp.json()
}

function getPackageFromHash() {
  const hash = location.hash.replace(/^#/, "")
  return decodeURIComponent(hash) || null
}

function showError(root, message) {
  root.innerHTML = `<div class="admonition failure"><p class="admonition-title">Error</p><p>${message}</p></div>`
}

function initTablesort(root) {
  if (typeof Tablesort === "undefined") return
  root.querySelectorAll("table:not([class]):not([data-no-sort])").forEach((t) => {
    const noSort = []
    t.querySelectorAll("th").forEach((th) => {
      if (th.textContent.trim() === "Version") {
        th.setAttribute("data-sort-method", "none")
        noSort.push(th)
      }
    })
    new Tablesort(t)
    noSort.forEach((th) => th.removeAttribute("role"))
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
    a.addEventListener("click", () => h.scrollIntoView({ behavior: "smooth" }))
    li.appendChild(a)
    list.appendChild(li)
  }

  nav.innerHTML = `<label class="md-nav__title" for="__toc">On this page</label>`
  nav.appendChild(list)
}
