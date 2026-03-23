document$.subscribe(async () => {
  const cells = document.querySelectorAll("td.pypi-downloads")
  if (!cells.length) return

  const fmt = new Intl.NumberFormat("en", { notation: "compact" })

  const now = new Date()
  // dataset updates on the 1st of each month; use previous month's key until then
  const key = now.getDate() >= 2
    ? `${now.getFullYear()}-${now.getMonth()}`
    : now.getMonth() === 0
      ? `${now.getFullYear() - 1}-11`
      : `${now.getFullYear()}-${now.getMonth() - 1}`
  const CACHE_KEY = `pypi-downloads-${key}`

  let downloads
  try {
    const cached = localStorage.getItem(CACHE_KEY)
    if (cached) {
      downloads = new Map(JSON.parse(cached))
    }
  } catch {
    // ignore storage errors
  }

  if (!downloads) {
    try {
      const resp = await fetch(
        "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.min.json"
      )
      if (!resp.ok) {
        console.error(`Failed to fetch PyPI download stats: ${resp.status} ${resp.statusText}`)
        return
      }
      const json = await resp.json()

      downloads = new Map()
      for (const row of json.rows) {
        downloads.set(row.project.toLowerCase(), row.download_count)
      }

      try {
        localStorage.setItem(
          CACHE_KEY,
          JSON.stringify(Array.from(downloads))
        )
      } catch {
        // ignore storage quota errors
      }
    } catch (err) {
      console.error("Failed to fetch PyPI download stats:", err)
      return
    }
  }

  for (const cell of cells) {
    const count = downloads.get(cell.dataset.package.toLowerCase())
    if (count != null) {
      cell.textContent = fmt.format(count)
      cell.setAttribute("data-sort", String(count))
    } else {
      console.warn(`No download data for ${cell.dataset.package}`)
    }
  }
})
