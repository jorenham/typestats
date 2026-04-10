document$.subscribe(() => {
  const bar = document.querySelector(".filter-bar")
  if (!bar) return

  const toggle = bar.querySelector(".filter-toggle")
  const panel = bar.querySelector(".filter-groups")
  toggle?.addEventListener("click", () => {
    const open = panel.classList.toggle("filter-groups--open")
    toggle.setAttribute("aria-expanded", String(open))
  })

  const groups = bar.querySelectorAll(".filter-group:not(.filter-group--range)")
  const rangeGroup = bar.querySelector(".filter-group--range[data-filter='coverage']")
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

  const thumbMin = rangeGroup?.querySelector('input[data-thumb="min"]')
  const thumbMax = rangeGroup?.querySelector('input[data-thumb="max"]')
  const labelMin = rangeGroup?.querySelector("[data-range-min]")
  const labelMax = rangeGroup?.querySelector("[data-range-max]")

  function inRange(v, lo, hi) {
    return v >= lo && v <= hi
  }

  function apply() {
    lockWidths()
    const active = {}
    for (const g of groups) {
      const btn = g.querySelector(".filter-btn--active")
      if (btn) active[g.dataset.filter] = btn.dataset.value
    }
    const lo = thumbMin ? +thumbMin.value : 0
    const hi = thumbMax ? +thumbMax.value : 100
    for (const row of rows) {
      let show = true
      for (const [key, val] of Object.entries(active))
        if (val !== "all" && row.dataset[toCamel(key)] !== val) show = false
      if (show && thumbMin)
        show =
          inRange(+row.dataset.coverage, lo, hi) &&
          inRange(+row.dataset.coverageStrict, lo, hi)
      row.style.display = show ? "" : "none"
    }
  }

  if (thumbMin && thumbMax) {
    for (const thumb of [thumbMin, thumbMax]) {
      thumb.addEventListener("input", () => {
        if (+thumbMin.value > +thumbMax.value)
          thumb.value = thumb === thumbMin ? thumbMax.value : thumbMin.value
        if (labelMin) labelMin.textContent = thumbMin.value + "%"
        if (labelMax) labelMax.textContent = thumbMax.value + "%"
        apply()
      })
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
