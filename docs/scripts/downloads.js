document$.subscribe(async () => {
  const cells = document.querySelectorAll("td.pypi-downloads")
  if (!cells.length) return

  const fmt = new Intl.NumberFormat("en", { notation: "compact" })

  try {
    const resp = await fetch(
      "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.min.json"
    )
    if (!resp.ok) {
      console.error(`Failed to fetch PyPI download stats: ${resp.status} ${resp.statusText}`)
      return
    }
    const json = await resp.json()

    const downloads = new Map()
    for (const row of json.rows) {
      downloads.set(row.project.toLowerCase(), row.download_count)
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
  } catch (err) {
    console.error("Failed to fetch PyPI download stats:", err)
  }
})
