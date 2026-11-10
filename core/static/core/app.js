// Buscador de cliente: escribe el RUT (con o sin puntos y guion) o parte del nombre.
(function () {
  const selector = document.getElementById("id_cliente");
  if (!selector) return;
  const todas = [...selector.options].map((o) => ({ valor: o.value, texto: o.text }));
  const simple = (t) => t.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  const soloRut = (t) => t.replace(/[.\-\s]/g, "").toLowerCase();
  const buscador = document.createElement("input");
  buscador.type = "search";
  buscador.placeholder = "Buscar por RUT o nombre";
  buscador.autocomplete = "off";
  buscador.style.marginBottom = "6px";
  selector.parentNode.insertBefore(buscador, selector);
  const aviso = document.createElement("div");
  aviso.className = "ayuda";
  selector.parentNode.insertBefore(aviso, selector.nextSibling);

  buscador.addEventListener("input", () => {
    const texto = buscador.value.trim();
    const elegido = selector.value;
    const porRut = /\d/.test(texto) ? soloRut(texto) : null;
    const calzan = todas.filter((o) => !o.valor || !texto || simple(o.texto).includes(simple(texto))
      || (porRut && soloRut(o.texto).includes(porRut)));
    selector.innerHTML = "";
    calzan.forEach((o) => selector.add(new Option(o.texto, o.valor)));
    const reales = calzan.filter((o) => o.valor);
    if (texto && reales.length === 1) selector.value = reales[0].valor;       // calce único: queda cargado
    else if (reales.some((o) => o.valor === elegido)) selector.value = elegido;
    else selector.value = "";
    aviso.textContent = !texto ? "" : reales.length === 0 ? "Ningún cliente guardado calza con esa búsqueda."
      : reales.length === 1 ? "Cliente cargado." : reales.length + " clientes calzan: elige uno de la lista.";
  });
  buscador.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
})();

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
