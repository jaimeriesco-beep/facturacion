// Editor de líneas de detalle: agregar filas, autocompletar desde el producto y totales en vivo.
(function () {
  const tabla = document.querySelector("#lineas");
  if (!tabla) return;
  const cuerpo = tabla.querySelector("tbody");
  const total = document.querySelector('input[name$="-TOTAL_FORMS"]');
  const productos = JSON.parse(document.getElementById("productos-js").textContent);
  const soloExento = tabla.dataset.soloExento === "1";
  const iva = Number(tabla.dataset.iva);
  const pesos = (n) => "$" + Math.round(n).toLocaleString("es-CL");
  const campo = (fila, nombre) => fila.querySelector(`[name$="-${nombre}"]`);

  function recalcular() {
    let neto = 0, exento = 0;
    cuerpo.querySelectorAll("tr").forEach((fila) => {
      const borrar = campo(fila, "DELETE");
      if (borrar && borrar.checked) return;
      const monto = Math.round((Number(campo(fila, "cantidad").value) || 0)
        * (Number(campo(fila, "precio").value) || 0)
        * (100 - (Number(campo(fila, "descuento").value) || 0)) / 100);
      if (soloExento || campo(fila, "exento").checked) exento += monto; else neto += monto;
    });
    const impuesto = Math.round(neto * iva / 100);
    document.getElementById("t-neto").textContent = pesos(neto);
    document.getElementById("t-exento").textContent = pesos(exento);
    document.getElementById("t-iva").textContent = pesos(impuesto);
    document.getElementById("t-total").textContent = pesos(neto + exento + impuesto);
  }

  cuerpo.addEventListener("change", (e) => {
    if (e.target.name && e.target.name.endsWith("-producto")) {
      const p = productos[e.target.value];
      if (p) {
        const fila = e.target.closest("tr");
        campo(fila, "descripcion").value = p.descripcion;
        campo(fila, "precio").value = p.precio;
        campo(fila, "exento").checked = p.exento || soloExento;
      }
    }
    recalcular();
  });
  cuerpo.addEventListener("input", recalcular);

  document.getElementById("agregar-linea").addEventListener("click", () => {
    const html = document.getElementById("linea-vacia").innerHTML
      .replace(/__prefix__/g, total.value);
    cuerpo.insertAdjacentHTML("beforeend", html);
    total.value = Number(total.value) + 1;
  });
  recalcular();
})();
