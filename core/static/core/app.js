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
  // Igual que en el servidor: coma decimal; el punto solo separa miles (1.250,5).
  function leer(texto) {
    let t = (texto || "").replace(/\s/g, "");
    if (!t) return 0;
    if (t.includes(".")) {
      const grupos = t.split(",")[0].replace("-", "").split(".");
      if (!/^\d{1,3}$/.test(grupos[0]) || !grupos.slice(1).every((g) => /^\d{3}$/.test(g))) return NaN;
      t = t.replace(/\./g, "");
    }
    if (!/^-?\d*,?\d*$/.test(t)) return NaN;
    return Number(t.replace(",", "."));
  }
  // Redondeo legal a pesos enteros (50 centavos o más sube), sin el ruido de los decimales binarios.
  const pesoEntero = (n) => Math.round(Number(n.toFixed(6)));
  const campo = (fila, nombre) => fila.querySelector(`[name$="-${nombre}"]`);

  function recalcular() {
    let neto = 0, exento = 0;
    cuerpo.querySelectorAll("tr").forEach((fila) => {
      const borrar = campo(fila, "DELETE");
      if (borrar && borrar.checked) return;
      const valores = ["cantidad", "precio", "descuento"].map((n) => {
        const casilla = campo(fila, n), v = leer(casilla.value);
        casilla.classList.toggle("invalido", Number.isNaN(v));
        return Number.isNaN(v) ? 0 : v;
      });
      const monto = pesoEntero(valores[0] * valores[1] * (100 - valores[2]) / 100);
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
  // Basurero de cada línea: la marca para eliminar al guardar y la saca de la vista.
  cuerpo.addEventListener("click", (e) => {
    const boton = e.target.closest(".quitar-linea");
    if (!boton) return;
    const fila = boton.closest("tr");
    campo(fila, "DELETE").checked = true;
    fila.hidden = true;
    recalcular();
  });

  document.getElementById("agregar-linea").addEventListener("click", () => {
    const html = document.getElementById("linea-vacia").innerHTML
      .replace(/__prefix__/g, total.value);
    cuerpo.insertAdjacentHTML("beforeend", html);
    total.value = Number(total.value) + 1;
  });
  recalcular();
})();

// Casillas numéricas: un punto usado como decimal (14.5) se cambia por coma (14,5) al salir.
document.addEventListener("focusout", (e) => {
  const c = e.target;
  if (!c.matches || !c.matches("input[data-decimal]")) return;
  const v = c.value.trim();
  if (/^-?\d+\.\d+$/.test(v) && !/^-?\d{1,3}(\.\d{3})+$/.test(v)) {
    c.value = v.replace(".", ",");
    c.dispatchEvent(new Event("input", { bubbles: true }));
  }
});
