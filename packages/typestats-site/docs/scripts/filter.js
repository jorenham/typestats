document$.subscribe(() => {
  const bar = document.querySelector(".filter-bar")
  if (!bar) return

  const toggle = bar.querySelector(".filter-toggle")
  const panel = bar.querySelector(".filter-groups")
  toggle?.addEventListener("click", () => {
    const open = panel.classList.toggle("filter-groups--open")
    toggle.setAttribute("aria-expanded", String(open))
  })

  const groups = bar.querySelectorAll(".filter-group")
  const table = bar.nextElementSibling
  const rows = table?.querySelectorAll("tbody tr[data-py-typed]")
  if (!rows?.length) return

  let widthsLocked = false
  function lockWidths() {
    if (widthsLocked) return
    widthsLocked = true
    for (const th of table.querySelectorAll("thead th"))
      th.style.width = th.offsetWidth + "px"
    table.style.tableLayout = "fixed"
  }

  function toCamel(s) {
    return s.replace(/-./g, m => m[1].toUpperCase())
  }

  function apply() {
    lockWidths()
    const active = {}
    for (const g of groups) {
      const btn = g.querySelector(".filter-btn--active")
      if (btn) active[g.dataset.filter] = btn.dataset.value
    }
    for (const row of rows) {
      let show = true
      for (const [key, val] of Object.entries(active))
        if (val !== "all" && row.dataset[toCamel(key)] !== val) show = false
      row.style.display = show ? "" : "none"
    }
  }

  for (const g of groups) {
    for (const btn of g.querySelectorAll(".filter-btn")) {
      btn.addEventListener("click", () => {
        const prev = g.querySelector(".filter-btn--active")
        if (prev === btn) return
        if (prev) {
          prev.classList.remove("filter-btn--active")
          prev.setAttribute("aria-pressed", "false")
        }
        btn.classList.add("filter-btn--active")
        btn.setAttribute("aria-pressed", "true")
        apply()
      })
    }
  }
})
