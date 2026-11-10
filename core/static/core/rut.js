// Las casillas de RUT se formatean solas mientras se escribe: 12.345.678-9
// Solo admiten números; el dígito verificador (el último) también puede ser K.
(function () {
  function formatear(texto) {
    const limpio = texto.toUpperCase().replace(/[^0-9K]/g, "");
    if (limpio.length <= 1) return limpio.replace("K", "");
    const cuerpo = limpio.slice(0, -1).replace(/K/g, "").slice(0, 8);
    const dv = limpio.slice(-1);
    if (!cuerpo) return dv === "K" ? "" : dv;
    return cuerpo.replace(/\B(?=(\d{3})+(?!\d))/g, ".") + "-" + dv;
  }
  document.querySelectorAll("input[data-rut]").forEach((casilla) => {
    casilla.value = formatear(casilla.value);
    casilla.addEventListener("input", () => { casilla.value = formatear(casilla.value); });
  });
})();
