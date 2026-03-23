document$.subscribe(() => {
  const tables = document.querySelectorAll(
    "article table:not([class]):not([data-no-sort])",
  )
  tables.forEach(table => {
    const noSortHeaders = []
    table.querySelectorAll("th").forEach(th => {
      if (th.textContent.trim() === "Version") {
        th.setAttribute("data-sort-method", "none")
        noSortHeaders.push(th)
      }
    })
    new Tablesort(table)
    noSortHeaders.forEach(th => th.removeAttribute("role"))
  })
})
