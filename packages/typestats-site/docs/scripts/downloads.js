document$.subscribe(() => {
  fillDownloadCells(document.querySelectorAll("td.pypi-downloads"))
})
